# Cisco configuration backup

Back up running configurations from Cisco IOS, IOS-XE, and NX-OS devices over SSH. The tool reads a YAML inventory, checks the server's SSH host key, and runs `show running-config` on each device. It keeps timestamped snapshots and logs the outcome of each device and the overall run.

## Setup

Use Python 3.11 or later on Linux or macOS. Run these commands from the project directory.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp inventory.example.yml inventory.yml
```

Edit `inventory.yml` with the management IP addresses or DNS names of your devices. Use a unique lowercase `name` for each device and keep it stable between runs. Names can include digits, hyphens, and underscores. Set `device_type` to `cisco_ios`, `cisco_xe`, or `cisco_nxos`. The optional `port` defaults to 22 and `credential_prefix` defaults to `CISCO`.

Use an account authorized for `show running-config` and Netmiko's session setup commands. Populate `~/.ssh/known_hosts` with the devices' keys after verifying their fingerprints through a trusted source. Use the same host spelling as the inventory. OpenSSH entries for a nondefault port use `[host]:port`. Pass `--known-hosts /path/to/known_hosts` to use a dedicated file. Unknown or changed host keys cause that device's backup to fail.

Provide credentials through environment variables. The example inventory uses both `CISCO` and `DC` prefixes. Set only the prefixes your inventory references. In Bash, you can enter secrets without echoing them or placing their values in command history.

```bash
read -r -p 'Cisco username: ' CISCO_USERNAME
read -r -s -p 'Cisco password: ' CISCO_PASSWORD
printf '\n'
export CISCO_USERNAME CISCO_PASSWORD

read -r -p 'Data center username: ' DC_USERNAME
read -r -s -p 'Data center password: ' DC_PASSWORD
printf '\n'
export DC_USERNAME DC_PASSWORD
```

If IOS or IOS-XE requires an enable password, provide the optional `<PREFIX>_ENABLE_SECRET` the same way and export it. NX-OS uses the privileges assigned to the login account.

## Run and verify

```sh
python backup.py --check-inventory
python backup.py
python -m unittest discover -s tests -v
```

Inventory validation runs without credentials or a device connection. The tests simulate device sessions. Override defaults with `--inventory`, `--backup-dir`, `--log-file`, `--known-hosts`, and `--read-timeout`. Run `python backup.py --help` for the full options. Relative paths resolve from the current directory.

A first successful capture or a changed configuration creates a file such as `backups/branch-router/20261121T090000123456Z.cfg`. The name uses UTC with microseconds. Comparisons ignore line-ending style and known volatile Cisco preamble lines, including capture timestamps. The saved snapshot contains the captured command output, with a final newline added if necessary. A stable configuration generates an `unchanged` log entry and keeps its previous snapshot.

The tool writes status to stderr and `logs/backup.log`. Exit code 0 means every device was saved or unchanged. Code 1 means at least one device failed and processing continued with the remaining devices. Code 2 indicates an argument or setup error. For SSH failures, check management reachability, the trusted host key, the credential prefix, and account permissions from the same host and account as the backup process.

Configurations can contain secrets and sensitive network details. Keep the backup and log directories on private storage with access restricted to the backup account. Review custom output paths before committing. Schedule retention and log rotation according to your storage policy.

## Schedule with cron

Install the project on a host with SSH access to the devices. Have the scheduler inject the environment variables from your secret manager, or source a private shell file containing the exports. For the file approach, create a file outside the repository readable only by the scheduler account (`chmod 600`), and use shell-safe quoting for the values. Save the verified host keys where the scheduler can read them.

For example, add the following line using `crontab -e`. Replace the absolute paths. It runs daily at 02:15 in the scheduler's local timezone. Use one writer per backup directory and set the schedule so runs finish before the next one starts.

```cron
15 2 * * * umask 077 && . /absolute/private/backup-env.sh && cd /absolute/cisco-config-backup && .venv/bin/python backup.py --known-hosts /absolute/private/known_hosts
```

Run the scheduled command manually under the scheduler account before relying on cron. Use the tool's log and exit status for monitoring. Configure log rotation separately.

## Publish to GitHub

Create an empty repository under your GitHub account or organization. Choose visibility and add a license of your choice if you want to grant reuse rights. From the project directory, inspect the staged files before committing.

```sh
git init -b main
git add .gitignore .github README.md backup.py inventory.example.yml requirements.txt tests
git diff --cached --stat
git diff --cached
git commit -m "Add Cisco configuration backup tool"
git remote add origin https://github.com/YOUR-ACCOUNT/YOUR-REPOSITORY.git
git push -u origin main
```

The provided GitHub Actions workflow runs unit tests and checks the sample inventory on pushes and pull requests. It requires no device credentials.

[Netmiko SSH platforms](https://github.com/ktbyers/netmiko/blob/develop/PLATFORMS.md)
