# Yet another computing scheduler & cloud orchestration engine
[![FOSSA Status](https://app.fossa.com/api/projects/git%2Bgithub.com%2Ftilde-lab%2Fyascheduler.svg?type=shield)](https://app.fossa.com/projects/git%2Bgithub.com%2Ftilde-lab%2Fyascheduler?ref=badge_shield)


**Yascheduler** is a simple job scheduler designed for submitting scientific
calculations and copying back the results from the computing clouds.

Currently it has been used for the _ab initio_ [CRYSTAL][crystal] code, although
any other scientific code can be supported via the declarative control template
system (see `yascheduler.conf` settings file). An example dummy C++ code with its
configuration template is included.

## Installation

Use `pip` and PyPI: `pip install yascheduler`.

The last updates and bugfixes can be obtained cloning the repository:

```
git clone https://github.com/tilde-lab/yascheduler.git
pip install yascheduler/
```

The installation procedure creates the configuration file located at
`/etc/yascheduler/yascheduler.conf`.
The file contains credentials for Postgres database access, used directories,
cloud providers and scientific simulation codes (called _engines_).
Please check and amend this file with the correct credentials. The database
and the system service should then be initialized with `yainit` script.

## Usage

```python
from configparser import ConfigParser

from yascheduler import CONFIG_FILE
from yascheduler.scheduler import Yascheduler

config = ConfigParser()
config.read(CONFIG_FILE)
yac = Yascheduler(config)

label = 'test assignment'
engine = 'pcrystal'
struct_input = str(...) # simulation control file: crystal structure
setup_input = str(...) # simulation control file: main setup, can include struct_input

result = yac.queue_submit_task(
    label, {"fort.34": struct_input, "INPUT": setup_input}, engine
)
print(result)
```

File paths can be set using the environment variables:

- `YASCHEDULER_CONF_PATH`

  Configuration file.

  _Default_: `/etc/yascheduler/yascheduler.conf`

- `YASCHEDULER_LOG_PATH`

  Log file path.

  _Default_: `/var/log/yascheduler.log`

- `YASCHEDULER_PID_PATH`

  PID file.

  _Default_: `/var/run/yascheduler.pid`

## Configuration File Reference

### Database Configuration `[db]`

Connection to a PostgreSQL database.

- `user`

  The username to connect to the PostgreSQL server with.

- `password`

  The user password to connect to the server with. This parameter is optional

- `host`

  The hostname of the PostgreSQL server to connect with.

- `port`

  The TCP/IP port of the PostgreSQL server instance.

  _Default_: `5432`

- `database`

  The name of the database instance to connect with.

  _Default_: Same as `user`

### Local Settings `[local]`

- `data_dir`

  Path to root directory of local data files.
  Can be relative to the current working directory.

  _Default_: `./data`

  _Example_: `/srv/yascheduler`

- `tasks_dir`

  Path to directory with tasks results.

  _Default_: `tasks` under `data_dir`

  _Example_: `%(data_dir)s/tasks`

- `keys_dir`

  Path to directory with SSH keys.

  _Default_: `keys` under `data_dir`

  _Example_: `%(data_dir)s/keys`

- `engines_dir`

  Path to directory with engines repository.

  _Default_: `engines` under `data_dir`

  _Example_: `%(data_dir)s/engines`

- `allocator_threads`

  Maximum number of node allocator threads.

  _Default_: `10`

- `deallocator_threads`

  Maximum number of node deallocator threads.

  _Default_: `2`

### Remote Settings `[remote]`

- `data_dir`

  Path to root directory of data files on remote node.
  Can be relative to the remote current working directory (usually `$HOME`).

  _Default_: `./data`

  _Example_: `/src/yascheduler`

- `tasks_dir`

  Path to directory with tasks results on remote node.

  _Default_: `tasks` under `data_dir`

  _Example_: `%(data_dir)s/tasks`

- `engines_dir`

  Path to directory with engines on remote node.

  _Default_: `engines` under `data_dir`

  _Example_: `%(data_dir)s/engines`

- `user`

  Default ssh username.

  _Default_: `root`

### Providers `[clouds]`

All cloud providers settings are set in the `[cloud]` group.
Each provider has its own settings prefix.

These settings are common to all the providers:

- `*_max_nodes`

  The maximum number of nodes for a given provider.

- `*_user`

  Per provider override of `remote.user`.

#### Hetzner

Settings prefix is `hetzner`.

- `hetzner_token`

  API token with Read & Write permissions for the project.

#### Azure

Azure Cloud should be pre-configured for `yascheduler`.

Create a dedicated _Enterprise Application_ for service.
Create an _Application Registration_.
Add _Client Secret_ to the Application Registration.

Create a dedicated _Resource Group_.
Assign roles _Network Contributor_ and _Virtual Machine Contributor_
in the _Resource Group_.

Settings prefix is `az`.

- `az_tenant_id`

  Tenant ID of Azure Active Directory.

- `az_client_id`

  Application ID.

- `az_client_secret`

  Client Secret value from Application Registration.

- `az_subscription_id`

  Subscription ID

- `az_resource_group`

  Resource Group name.

  _Default_: `YaScheduler-VM-rg`

- `az_user`

  SSH username. `root` is not supported.

- `az_location`

  Default location for resources.

  _Default_: `westeurope`

- `az_infra_tmpl_path`

  Path to deployment template of common parts.

  _Default_: `azure_infra_tmpl.json`

- `az_infra_param_subnetMask`

  Subnet mask of VMs network.

  _Default_: `20`

- `az_infra_param_*`

  Any input of deployment template of common parts.
  Defaults from deployment manifest.

- `az_vm_tmpl_path`

  Path to deployment template of VM.

  _Default_: `azure_vm_tmpl.json`

- `az_vm_param_virtualMachineSize`

  Machine type.

  _Default_: `Standard_B1s`

- `az_vm_param_*`

  Any input of deployment template of VM.
  Defaults from deployment manifest.

- `az_vm_param_osDiskSize`

  Root disk type.

  _Default_: `StandardSSD_LRS`

- `az_vm_param_imagePublisher`

  OS image publisher.

  _Default_: `debian`

- `az_vm_param_imageOffer`

  OS image offer.

  _Default_: `debian-10`

- `az_vm_param_imageSku`

  OS image SKU.

  _Default_: `10-backports-gen2`

- `az_vm_param_imageVersion`

  OS image version.

  _Default_: `latest`

#### UpCloud

Settings prefix is `upcloud`.

- `upcloud_login`

  Username.

- `upcloud_password`

  Password.

#### Engines `[engine.*]`

Every engine defined in section `[engine.name]`, where `name` is engine's name.
Name can be any alphanumeric but can't changed later.

- `platform`

  List of supported platform, separated by space or newline.

  _Default_: `debian-10`
  _Example_: `mY-cOoL-OS another-cool-os`

- `platform_packages`

  A list of required packages, separated by space or newline, which
  will be installed by the system package manager.

  _Default_: []
  _Example_: `openmpi-bin wget`

- `deploy_local_files`

  A list of filenames, separated by space or newline, which will be copied
  from local `%(engines_dir)s/%(engine_name)s` to remote
  `%(engines_dir)s/%(engine_name)s`.
  Conflicts with `deploy_local_archive` and `deploy_remote_archive`.

  _Example_: `dummyengine`

- `deploy_local_archive`

  A name of the local archive (`.tar.gz`) which will be copied
  from local `%(engines_dir)s/%(engine_name)s` to the remote machine and
  then unarchived to the `%(engines_dir)s/%(engine_name)s`.
  Conflicts with `deploy_local_archive` and `deploy_remote_archive`.

  _Example_: `dummyengine.tar.gz`

- `deploy_remote_archive`

  The url to the engine arhive (`.tar.gz`) which will be downloaded
  to the remote machine and then unarchived to the
  `%(engines_dir)s/%(engine_name)s`.
  Conflicts with `deploy_local_archive` and `deploy_remote_archive`.

  _Example_: `https://example.org/dummyengine.tar.gz`

- `spawn`

  Command that starts the task on the remote machine.
  Command is executed in background subshell with nohup.
  Current working directory is task's directory.
  Command can be templated:

  - `{task_path}` - path to the task's directory
  - `{engine_path}` - path to the engine's directory
  - `{ncpus}` - number of CPU cores

  _Example_: `cp {task_path}/INPUT OUTPUT && mpirun -np {ncpus} --allow-run-as-root -wd {task_path} {engine_path}/Pcrystal >> OUTPUT 2>&1`
  _Example_: `{engine_path}/gulp < INPUT > OUTPUT`

- `check_pname`

  Process name used to check that task is still running.
  Conflicts with `check_cmd`.

  _Example_: `dummyengine`

- `check_cmd`

  Command used to check that task is still running.
  Conflicts with `check_pname`. See also `check_cmd_code`.

  _Example_: `ps ax -ocomm= | grep -q dummyengine`

- `check_cmd_code`

  Expected exit code of command from `check_cmd`.
  If code matches than task is running.

  _Default_: `0`

- `sleep_interval`

  Interval in seconds between task checks.
  Set to a higher value if you are expecting a long running job.

  _Default_: `1`

- `input_files`

  A list of task input file names, separated by a space or new line,
  that will be copied to the remote directory of the task before it is started.

  _Example_: `INPUT sibling.file`

- `output_files`

  A list of task output file names, separated by a space or new line,
  that will be copied from the remote directory of the task after it is finished.

  _Example_: `INPUT OUTPUT`

## Aiida Integration

See the detailed instructions for the [MPDS-AiiDA-CRYSTAL workflows][mpds-aiida]
as well as the [ansible-mpds][ansible-aiida] repository. In essence:

```
pip install --upgrade paramiko
ssh aiidauser@localhost # important
reentry scan
verdi computer setup
verdi computer test $COMPUTER
verdi code setup
```

[ansible-aiida]: https://github.com/mpds-io/ansible-mpds
[crystal]: http://www.crystal.unito.it
[mpds-aiida]: https://github.com/mpds-io/mpds-aiida


## License
[![FOSSA Status](https://app.fossa.com/api/projects/git%2Bgithub.com%2Ftilde-lab%2Fyascheduler.svg?type=large)](https://app.fossa.com/projects/git%2Bgithub.com%2Ftilde-lab%2Fyascheduler?ref=badge_large)