import os
import paramiko

class SaltstackDeployer:
    """SFTPs pipeline template to /tmp/ and moves it to the Salt directory with configurable permissions."""

    def __init__(self, config: dict):
        self.hostname = os.environ["TAILSCALE_NODE"]
        # SSH credentials for the Tailscale connection are separate from the
        # SO_USER/SO_PASS Elasticsearch basic-auth credentials used elsewhere.
        self.username = os.environ.get("TS_USER", "admin")
        self.password = os.environ.get("TS_PASS") or None
        self.tmp_dir = config["saltstack"]["tmp_dir"]
        self.dest_dir = config["saltstack"]["destination_dir"]
        self.file_owner = config["saltstack"].get("file_owner", "root:root")

    def deploy_persistently(self, pipeline_name: str, local_file_path: str):
        """Connects via Paramiko, uploads file, and performs sudo move with configured owner.

        Remote filename is `{pipeline_name}.json` — matches the sudoers wildcard
        grant (`/tmp/*.json` -> `.../ingest/*.json`) already configured for
        so_custom_ingest_pipeline-style deploys.
        """
        self._deploy_persistently_as(f"{pipeline_name}.json", local_file_path)

    def deploy_persistently_exact(self, remote_filename: str, local_file_path: str):
        """Same as deploy_persistently(), but uses remote_filename exactly as
        given -- no ".json" suffix appended.

        Needed for Security-Onion-native pipeline files like "global@custom"
        that so-elasticsearch-pipelines pushes to Elasticsearch using the
        filename itself as the pipeline name (no extension). Requires its own
        sudoers grant scoped to that exact filename -- the existing `*.json`
        wildcard grant does not match it. See usage.md Component 5 section.
        """
        self._deploy_persistently_as(remote_filename, local_file_path)

    def _deploy_persistently_as(self, remote_filename: str, local_file_path: str):
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        if self.password:
            ssh.connect(self.hostname, username=self.username, password=self.password, timeout=15)
        else:
            ssh.connect(self.hostname, username=self.username, timeout=15)

        # 1. SFTP file to /tmp/
        sftp = ssh.open_sftp()
        tmp_file = os.path.join(self.tmp_dir, remote_filename)
        sftp.put(local_file_path, tmp_file)
        sftp.close()

        # 2. Exec SSH command to move to Salt local directory with parameterized chown
        final_dest = os.path.join(self.dest_dir, remote_filename)
        cmd = f"sudo mv {tmp_file} {final_dest} && sudo chown {self.file_owner} {final_dest}"
        stdin, stdout, stderr = ssh.exec_command(cmd)

        exit_status = stdout.channel.recv_exit_status()
        if exit_status != 0:
            err_msg = stderr.read().decode("utf-8")
            raise RuntimeError(f"Failed persistent Saltstack transfer: {err_msg}")

        ssh.close()
