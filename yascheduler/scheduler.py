#!/usr/bin/env python

import json
import logging
import os
import queue
import random
import string
from configparser import ConfigParser
from datetime import datetime, timedelta
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

import pg8000
from plumbum.commands.processes import CommandNotFound, ProcessExecutionError

from yascheduler import connect_db, CONFIG_FILE, SLEEP_INTERVAL, N_IDLE_PASSES
import yascheduler.clouds
from yascheduler.engine import (
    Engine,
    EngineRepository,
    LocalFilesDeploy,
    LocalArchiveDeploy,
    RemoteArchiveDeploy,
)
from yascheduler.ssh import MyParamikoMachine
from yascheduler.time import sleep_until
from yascheduler.webhook_worker import WebhookWorker, WebhookTask

logging.basicConfig(level=logging.INFO)


class Yascheduler:

    STATUS_TO_DO = 0
    STATUS_RUNNING = 1
    STATUS_DONE = 2

    _log: logging.Logger
    _webhook_queue: "queue.Queue[WebhookTask]"
    _webhook_threads: List[WebhookWorker]
    clouds: Optional["yascheduler.clouds.CloudAPIManager"] = None
    connection: pg8000.Connection
    cursor: pg8000.Cursor
    engines: EngineRepository
    local_engines_dir: Path
    local_data_dir: Path
    local_keys_dir: Path
    local_tasks_dir: Path
    remote_engines_dir: Path
    remote_data_dir: Path
    remote_tasks_dir: Path
    remote_machines: Dict[str, MyParamikoMachine]
    ssh_user: str

    def __init__(self, config: ConfigParser, logger: Optional[logging.Logger] = None):
        if logger:
            self._log = logger.getChild(self.__class__.__name__)
        else:
            self._log = logging.getLogger(self.__class__.__name__)

        local_cfg = config["local"]
        self.local_data_dir = Path(local_cfg.get("data_dir", "./data")).resolve()
        self.local_engines_dir = Path(
            local_cfg.get("engines_dir", str(self.local_data_dir / "engines"))
        ).resolve()
        self.local_tasks_dir = Path(
            local_cfg.get("tasks_dir", str(self.local_data_dir / "tasks"))
        ).resolve()
        self.local_keys_dir = Path(
            local_cfg.get("keys_dir", str(self.local_data_dir / "keys"))
        ).resolve()

        remote_cfg = config["remote"]
        self.remote_data_dir = Path(remote_cfg.get("data_dir", "./data"))
        self.remote_engines_dir = Path(
            remote_cfg.get("engines_dir", str(self.remote_data_dir / "engines"))
        )
        self.remote_tasks_dir = Path(
            remote_cfg.get("tasks_dir", str(self.remote_data_dir / "tasks"))
        )

        self.connection, self.cursor = connect_db(config)
        self.remote_machines = {}
        self.ssh_user = remote_cfg.get("user", fallback="root")
        self.engines = self._load_engines(config)

        self._webhook_queue = queue.Queue()
        webhook_thread_num = int(local_cfg.get("webhook_threads", "2"))
        self._webhook_threads = []
        for i in range(webhook_thread_num):
            t = WebhookWorker(
                name=f"WebhookThread[{i}]",
                logger=self._log,
                task_queue=self._webhook_queue,
            )
            self._webhook_threads.append(t)

    def _load_engines(self, cfg: ConfigParser) -> EngineRepository:
        engines = EngineRepository()
        for section_name in cfg.sections():
            if not section_name.startswith("engine."):
                continue
            section = cfg[section_name]
            engine = Engine.from_config(section)
            engines[engine.name] = engine

        if not engines:
            raise RuntimeError("No engines were set up")

        return engines

    def start(self) -> None:
        for t in self._webhook_threads:
            t.start()

    def queue_get_resources(self):
        self.cursor.execute("SELECT ip, ncpus, enabled, cloud FROM yascheduler_nodes;")
        return self.cursor.fetchall()

    def queue_get_resource(self, ip):
        self.cursor.execute(
            """
            SELECT ip, ncpus, enabled, cloud
            FROM yascheduler_nodes
            WHERE ip=%s;
            """,
            [ip],
        )
        return self.cursor.fetchone()

    def queue_get_task(self, task_id):
        self.cursor.execute(
            """
            SELECT label, metadata, ip, status
            FROM yascheduler_tasks
            WHERE task_id=%s;
            """,
            [task_id],
        )
        row = self.cursor.fetchone()
        if not row:
            return None
        return dict(
            task_id=task_id,
            label=row[0],
            metadata=row[1],
            ip=row[2],
            status=row[3],
        )

    def queue_get_tasks_to_do(self, num_nodes):
        self.cursor.execute(
            """
            SELECT task_id, label, metadata
            FROM yascheduler_tasks
            WHERE status=%s LIMIT %s;
            """,
            (self.STATUS_TO_DO, num_nodes),
        )
        return [
            dict(task_id=row[0], label=row[1], metadata=row[2])
            for row in self.cursor.fetchall()
        ]

    def queue_get_tasks(self, jobs=None, status=None):
        if jobs is not None and status is not None:
            raise ValueError("jobs can be selected only by status or by task ids")
        if jobs is None and status is None:
            raise ValueError("jobs can only be selected by status or by task ids")
        if status is not None:
            query_string = "status IN ({})".format(", ".join(["%s"] * len(status)))
            params = status
        else:
            query_string = "task_id IN ({})".format(", ".join(["%s"] * len(jobs)))
            params = jobs

        sql_statement = """
        SELECT task_id, label, ip, status FROM yascheduler_tasks WHERE {};
        """.format(
            query_string
        )
        self.cursor.execute(sql_statement, params)
        return [
            dict(task_id=row[0], label=row[1], ip=row[2], status=row[3])
            for row in self.cursor.fetchall()
        ]

    def enqueue_task_event(self, task_id: int) -> None:
        task = self.queue_get_task(task_id) or {}
        wt = WebhookTask.from_dict(task)
        self._webhook_queue.put(wt)

    def queue_set_task_running(self, task_id, ip):
        self.cursor.execute(
            "UPDATE yascheduler_tasks SET status=%s, ip=%s WHERE task_id=%s;",
            (self.STATUS_RUNNING, ip, task_id),
        )
        self.connection.commit()
        self.enqueue_task_event(task_id)

    def queue_set_task_done(self, task_id, metadata):
        self.cursor.execute(
            """
            UPDATE yascheduler_tasks
            SET status=%s, metadata=%s
            WHERE task_id=%s;
            """,
            (self.STATUS_DONE, json.dumps(metadata), task_id),
        )
        self.connection.commit()
        self.enqueue_task_event(task_id)
        # if self.clouds:
        # TODO: free-up CloudAPIManager().tasks

    def queue_submit_task(self, label: str, metadata: Dict[str, Any], engine_name: str):
        if engine_name not in self.engines:
            raise RuntimeError("Engine %s requested, but not supported" % engine_name)

        for input_file in self.engines[engine_name].input_files:
            if input_file not in metadata:
                raise RuntimeError("Input file %s was not provided" % input_file)

        metadata["engine"] = engine_name
        rnd_str = "".join([random.choice(string.ascii_lowercase) for _ in range(4)])
        metadata["remote_folder"] = str(
            self.remote_tasks_dir
            / "{}_{}".format(datetime.now().strftime("%Y%m%d_%H%M%S"), rnd_str)
        )

        self.cursor.execute(
            """
            INSERT INTO yascheduler_tasks (label, metadata, ip, status)
            VALUES ('{label}', '{metadata}', NULL, {status})
            RETURNING task_id;""".format(
                label=label,
                metadata=json.dumps(metadata).replace("'", "''"),
                status=self.STATUS_TO_DO,
            )
        )
        self.connection.commit()
        self._log.info(":::submitted: %s" % label)
        return self.cursor.fetchone()[0]

    def ssh_connect(self, new_nodes):
        old_nodes = self.remote_machines.keys()

        ip_cloud_map = {}
        resources = self.queue_get_resources()
        for row in resources:
            if row[0] in new_nodes:
                ip_cloud_map[row[0]] = row[3]

        for ip in set(old_nodes) - set(new_nodes):
            self.remote_machines[ip].close()
            del self.remote_machines[ip]
        for ip in set(new_nodes) - set(old_nodes):
            cloud = self.clouds and self.clouds.apis.get(ip_cloud_map.get(ip))
            ssh_user = cloud and cloud.ssh_user or self.ssh_user
            self.remote_machines[ip] = MyParamikoMachine.create_machine(
                host=ip,
                user=ssh_user,
                keys_dir=self.local_keys_dir,
            )

        self._log.info("Nodes to watch: %s" % ", ".join(self.remote_machines.keys()))
        if not self.remote_machines:
            self._log.warning("No nodes set!")
        return True

    def ssh_run_task(self, ip, ncpus, label, metadata):
        # TODO handle this situation
        assert not self.ssh_node_busy_check(
            ip
        ), f"""
            Cannot run the task {label} at host {ip}, as this host is already
            occupied with another task!
            """

        assert metadata["remote_folder"]
        engine = self.engines.get(metadata["engine"])
        assert engine

        machine = self.remote_machines[ip]
        task_dir = machine.path(metadata["remote_folder"])
        try:
            if not task_dir.exists():
                task_dir.mkdir(parents=True)
            for input_file in engine.input_files:
                r_input_file = task_dir.join(input_file)
                r_input_file.write(metadata[input_file], encoding="utf-8")

            # detect cpus
            if not ncpus:
                ncpus = machine.cmd.nproc("--all").strip()

            # resolve paths
            engine_path = machine.path(self.remote_engines_dir / engine.name)
            task_path = machine.path(metadata["remote_folder"])

            # placeholders {task_path}, {engine_path} and {ncpus} are supported
            run_cmd = engine.spawn.format(
                engine_path=str(engine_path),
                task_path=str(task_path),
                ncpus=ncpus,
            )
            self._log.debug(run_cmd)

            r_nohup = machine.cmd.nohup
            r_sh = machine.cmd.sh
            r_nohup[r_sh, "-c", run_cmd].with_cwd(task_dir).run_bg()
        except Exception as err:
            self._log.error("SSH spawn cmd error: %s" % err)
            return False

        return True

    def ssh_node_busy_check(self, ip):
        assert ip in self.remote_machines.keys(), (
            f"Node {ip} was referred by active task," " however absent in node list"
        )
        machine = self.remote_machines[ip]

        for engine in self.engines.values():
            if engine.check_pname:
                for _ in machine.pgrep(engine.check_pname):
                    return True
            if engine.check_cmd:
                try:
                    code = machine.cmd.sh["-c", engine.check_cmd].run_retcode()
                    if code == engine.check_cmd_code:
                        return True
                except ProcessExecutionError as e:
                    self._log.info(f"Node {ip} failed command: {e}")
        return False

    def ssh_get_task(
        self, ip, engine_name, work_folder, store_folder: Path, remove=True
    ):
        machine = self.remote_machines[ip]
        r_work_folder = machine.path(work_folder)
        engine = self.engines[engine_name]
        for output_file in engine.output_files:
            try:
                machine.download(
                    r_work_folder.join(output_file),
                    store_folder / output_file,
                )
            except IOError as err:
                # TODO handle that situation properly
                self._log.error(
                    "Cannot scp %s/%s: %s" % (work_folder, output_file, err)
                )
                if "Connection timed out" in str(err):
                    break

        if remove:
            r_work_folder.delete()

    def clouds_allocate(self, on_task):
        if self.clouds:
            self.clouds.allocate(on_task)

    def clouds_deallocate(self, ips):
        if self.clouds:
            self.clouds.deallocate(ips)

    def clouds_get_capacity(self, resources):
        if self.clouds:
            return self.clouds.get_capacity(resources)
        return 0

    def setup_node(self, ip: str, user: str) -> None:
        """Provision a debian-like node"""

        engines = self.engines.filter_platforms(["debian-10"])
        if not engines:
            self._log.error("There is not supported engines!")
            return

        machine = MyParamikoMachine.create_machine(
            host=ip,
            user=user,
            keys_dir=self.local_keys_dir,
        )

        # print OS version
        result = machine.session().run("source /etc/os-release; echo $PRETTY_NAME")
        self._log.info("OS: {}".format(result[1].strip()))

        # print CPU count
        nproc = machine.cmd.nproc("--all").strip()
        self._log.info("CPUs count: {}".format(nproc))

        # install packages
        apt_get = machine["apt-get"]["-o", "DPkg::Lock::Timeout=600", "-y"]
        if user != "root":
            apt_get = machine.cmd.sudo[apt_get]
        self._log.info(f"Update packages...")
        apt_get("update")
        apt_get("upgrade")

        pkgs = engines.get_platform_packages()
        if pkgs:
            self._log.info("Install packages: {} ...".format(" ".join(pkgs)))
            apt_get("install", *pkgs)

        # print MPI version
        try:
            result = machine.get("mpirun")("--allow-run-as-root", "-V")
            self._log.info(result.split("\n")[0])
        except CommandNotFound:
            pass

        for engine in engines.values():
            self._log.info(f"Setup {engine.name} engine...")
            local_engine_dir = self.local_engines_dir / engine.name
            remote_engine_dir = machine.path(self.remote_engines_dir).join(engine.name)
            remote_engine_dir.mkdir(parents=True)
            for deployment in engine.deployable:
                # uploading binary from local; requires broadband connection
                if isinstance(deployment, LocalFilesDeploy):
                    for filepath in deployment.files:
                        rfpath = remote_engine_dir.join(filepath)
                        self._log.info(f"Uploading {filepath} to {rfpath}")
                        machine.upload(local_engine_dir / filepath, rfpath)
                        machine.cmd.chmod("+x", rfpath)

                # upload local archive
                # binary may be gzipped, without subfolders,
                # with an arbitrary archive name
                if isinstance(deployment, LocalArchiveDeploy):
                    fn = deployment.filename
                    apath = local_engine_dir / fn
                    rpath = remote_engine_dir.join(fn)
                    self._log.info(f"Uploading {fn} to {rpath}...")
                    machine.upload(apath, rpath)
                    self._log.info(f"Unarchiving {fn}...")
                    tar = machine.cmd.tar
                    tar["xfv", fn].with_cwd(remote_engine_dir).run()
                    rpath.delete()

                # downloading binary from a trusted non-public address
                if isinstance(deployment, RemoteArchiveDeploy):
                    url = deployment.url
                    fn = "archive.tar.gz"
                    rpath = remote_engine_dir.join(fn)
                    self._log.info(f"Downloading {url} to {rpath}...")
                    wget = machine.cmd.wget
                    wget(url, "-O", rpath)
                    self._log.info(f"Unarchiving {fn}...")
                    tar = machine.cmd.tar
                    tar["xfv", fn].with_cwd(remote_engine_dir).run()
                    rpath.delete()

    def stop(self):
        self._log.info("Stopping threads...")
        for t in self._webhook_threads:
            t.stop()
            t.join()


def daemonize(log_file=None):
    logger = get_logger(log_file)
    config = ConfigParser()
    config.read(CONFIG_FILE)

    yac = Yascheduler(config)
    clouds = yascheduler.clouds.CloudAPIManager(config, logger=logger)
    yac.clouds = clouds
    clouds.yascheduler = yac

    logging.getLogger("Yascheduler").setLevel(logging.DEBUG)
    clouds.initialize()
    yac.start()

    chilling_nodes = Counter()  # ips vs. their occurences

    logger.debug(
        "Available computing engines: %s"
        % ", ".join([engine_name for engine_name in yac.engines])
    )

    def step():
        resources = yac.queue_get_resources()
        all_nodes = [
            item[0] for item in resources if "." in item[0]
        ]  # NB provision nodes have fake ips
        if sorted(yac.remote_machines.keys()) != sorted(all_nodes):
            yac.ssh_connect(all_nodes)

        enabled_nodes: Dict[str, int] = {
            item[0]: item[1] for item in resources if item[2]
        }
        free_nodes = list(enabled_nodes.keys())

        # (I.) Tasks de-allocation clause
        tasks_running = yac.queue_get_tasks(status=(yac.STATUS_RUNNING,))
        logger.debug("running %s tasks: %s" % (len(tasks_running), tasks_running))
        for task in tasks_running:
            if yac.ssh_node_busy_check(task["ip"]):
                try:
                    free_nodes.remove(task["ip"])
                except ValueError:
                    pass
            else:
                ready_task = yac.queue_get_task(task["task_id"])
                webhook_url = ready_task["metadata"].get("webhook_url")
                local_folder = ready_task["metadata"].get("local_folder")
                remote_folder = ready_task["metadata"]["remote_folder"]
                if local_folder:
                    store_folder = Path(local_folder)
                else:
                    store_folder = yac.local_tasks_dir / Path(remote_folder).name
                store_folder.mkdir(parents=True, exist_ok=True)
                yac.ssh_get_task(
                    ready_task["ip"],
                    ready_task["metadata"]["engine"],
                    ready_task["metadata"]["remote_folder"],
                    store_folder,
                )
                ready_task["metadata"] = dict(
                    remote_folder=ready_task["metadata"]["remote_folder"],
                    local_folder=str(store_folder),
                )
                if webhook_url:
                    ready_task["metadata"]["webhook_url"] = webhook_url
                yac.queue_set_task_done(ready_task["task_id"], ready_task["metadata"])
                logger.info(
                    ":::task_id={} {} done and saved in {}".format(
                        task["task_id"],
                        ready_task["label"],
                        ready_task["metadata"].get("local_folder"),
                    )
                )
                # TODO here we might want to notify our data consumers in an event-driven manner
                # TODO but how to do it quickly or in the background?

        # (II.) Resourses and tasks allocation clause
        clouds_capacity = yac.clouds_get_capacity(resources)
        if free_nodes or clouds_capacity:
            for task in yac.queue_get_tasks_to_do(clouds_capacity + len(free_nodes)):
                if not free_nodes:
                    yac.clouds_allocate(task["task_id"])
                    continue
                random.shuffle(free_nodes)
                ip = free_nodes.pop()
                logger.info(
                    ":::submitting task_id=%s %s to %s"
                    % (task["task_id"], task["label"], ip)
                )

                if yac.ssh_run_task(
                    ip, enabled_nodes[ip], task["label"], task["metadata"]
                ):
                    yac.queue_set_task_running(task["task_id"], ip)

        # (III.) Resourses de-allocation clause
        if free_nodes:  # candidates for removal
            chilling_nodes.update(free_nodes)
            deallocatable = Counter(
                [
                    x[0]
                    for x in filter(
                        lambda x: x[1] >= N_IDLE_PASSES,
                        chilling_nodes.most_common(),
                    )
                ]
            )
            if deallocatable:
                yac.clouds_deallocate(list(deallocatable.elements()))
                chilling_nodes.subtract(deallocatable)

        # process results of allocators
        clouds.do_async_work()

        # print stats
        nodes = yac.queue_get_resources()
        enabled_nodes = list(filter(lambda x: x[2], nodes))
        logger.info(
            "NODES:\tenabled: %s\ttotal: %s",
            str(len(enabled_nodes)),
            str(len(nodes)),
        )
        logger.info(
            "TASKS:\trunning: %s\tto do: %s\tdone: %s",
            len(yac.queue_get_tasks(status=(yac.STATUS_RUNNING,))),
            len(yac.queue_get_tasks(status=(yac.STATUS_TO_DO,))),
            len(yac.queue_get_tasks(status=(yac.STATUS_DONE,))),
        )

    # The main scheduler loop
    try:
        while True:
            end_time = datetime.now() + timedelta(seconds=SLEEP_INTERVAL)
            step()
            sleep_until(end_time)
    except KeyboardInterrupt:
        clouds.stop()
        yac.stop()


def get_logger(log_file):
    logger = logging.getLogger("yascheduler")
    logger.setLevel(logging.DEBUG)

    if log_file:
        fh = logging.FileHandler(log_file)
        fh.setLevel(logging.DEBUG)
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        fh.setFormatter(formatter)
        logger.addHandler(fh)

    return logger


if __name__ == "__main__":
    daemonize()
