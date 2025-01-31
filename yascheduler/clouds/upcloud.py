import time
from configparser import ConfigParser
from typing import Dict

from upcloud_api import CloudManager, Server, Storage, ZONE, login_user_block

from yascheduler.clouds import AbstractCloudAPI


class UpCloudAPI(AbstractCloudAPI):

    name = "upcloud"

    client: CloudManager

    def __init__(self, config: ConfigParser):
        super().__init__(
            config=config,
            max_nodes=config.getint("clouds", "upcloud_max_nodes", fallback=None),
        )
        self.client = CloudManager(
            config.get("clouds", "upcloud_login"),
            config.get("clouds", "upcloud_pass"),
        )
        self.client.authenticate()

    def create_node(self):
        login_user = login_user_block(
            username=self.ssh_user,
            ssh_keys=[self.public_key] if self.public_key else [],
            create_password=False,
        )
        server = self.client.create_server(
            Server(
                core_number=8,
                memory_amount=4096,
                hostname=self.get_rnd_name("node"),
                zone=ZONE.London,
                storage_devices=[Storage(os="Debian 10.0", size=40)],
                login_user=login_user,
            )
        )
        ip = server.get_public_ip()
        self._log.info("CREATED %s" % ip)
        self._log.info("WAITING FOR START...")
        time.sleep(30)
        self._run_ssh_cmd_with_backoff(ip, cmd="whoami", max_time=60, max_interval=5)

        return ip

    def delete_node(self, ip):
        for server in self.client.get_servers():
            if server.get_public_ip() == ip:
                server.stop()
                self._log.info("WAITING FOR STOP...")
                time.sleep(20)
                while True:
                    try:
                        server.destroy()
                    except:
                        time.sleep(5)
                    else:
                        break
                for storage in server.storage_devices:
                    storage.destroy()
                self._log.info("DELETED %s" % ip)
                break
        else:
            self._log.info("NODE %s NOT DELETED AS UNKNOWN" % ip)
