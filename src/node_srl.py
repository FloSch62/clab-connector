import logging
import socket
import os
import re
import tempfile
import src.helpers as helpers

from paramiko import (
    SSHClient,
    BadHostKeyException,
    AuthenticationException,
    SSHException,
    AutoAddPolicy,
)

from src.node import Node

# set up logging
logger = logging.getLogger(__name__)


class SRLNode(Node):
    # this can be made part of the command line arguments, but this is not done (yet)
    SRL_USERNAME = "admin"
    SRL_PASSWORD = "NokiaSrl1!"
    NODE_TYPE = "srlinux"
    GNMI_PORT = "57400"
    VERSION_PATH = ".system.information.version"
    YANG_PATH = (
        "https://eda-asvr/eda-system/schemaprofiles/srlinux-ghcr-{version}/srlinux-{version}.zip"
    )
    SRL_IMAGE = "eda-system/srlimages/srlinux-{version}-bin/srlinux.bin"
    SRL_IMAGE_MD5 = "eda-system/srlimages/srlinux-{version}-bin/srlinux.bin.md5"

    def __init__(self, name, kind, node_type, version, mgmt_ipv4):
        super().__init__(name, kind, node_type, version, mgmt_ipv4)

    def test_ssh(self):
        """
        Tests the SSH connectivity to the node

        Returns
        -------
        True if the SSH was successful, False otherwise
        """
        logger.debug(
            f"Testing whether SSH works for node '{self.name}' with IP {self.mgmt_ipv4}"
        )
        ssh = SSHClient()
        ssh.set_missing_host_key_policy(AutoAddPolicy())

        try:
            ssh.connect(
                self.mgmt_ipv4, username=self.SRL_USERNAME, password=self.SRL_PASSWORD
            )
            logger.info(
                f"SSH test to {self.kind} node '{self.name}' with IP {self.mgmt_ipv4} was successful"
            )
            return True
        except (
            BadHostKeyException,
            AuthenticationException,
            SSHException,
            socket.error,
        ) as e:
            logger.critical(f"Could not connect to node {self}, exception: {e}")
            raise e

    def get_default_node_type(self):
        """
        Allows to override the default node type, if no type was provided
        """
        return "ixrd3l"

    def get_platform(self):
        """
        Platform name to be used in the bootstrap node resource
        """
        t = self.node_type.replace("ixr", "")
        return f"7220 IXR-{t.upper()}"

    def get_profile_name(self, topology):
        """
        Returns an EDA-safe name for a node profile
        """
        return f"{topology.get_eda_safe_name()}-{self.NODE_TYPE}-{self.version}"

    def is_eda_supported(self):
        """
        Returns True if this node is supported as part of an EDA topology
        """
        return True

    def get_interface_name_for_kind(self, ifname):
        """
        Converts the containerlab name of an interface to the node's naming convention

        Parameters
        ----------
        ifname: name of the interface as specified in the containerlab topology file

        Returns
        -------
        The name of the interface as accepted by the node
        """
        pattern = re.compile("^e([0-9])-([0-9]+)$")

        if pattern.match(ifname):
            match = pattern.search(ifname)
            return f"ethernet-{match.group(1)}-{match.group(2)}"

        return ifname

    def get_node_profile(self, topology):
        """
        Creates a node profile for this node kind & version

        Returns
        -------
        the rendered node-profile jinja template
        """
        logger.info(f"Rendering node profile for {self}")
        data = {
            "profile_name": self.get_profile_name(topology),
            "sw_version": self.version,
            "gnmi_port": self.GNMI_PORT,
            "operating_system": self.kind,
            "version_path": self.VERSION_PATH,
            # below evaluates to something like v24\.7\.1.*
            "version_match": "v{}.*".format(self.version.replace(".", "\.")),
            "yang_path": self.YANG_PATH.format(version=self.version),
            "node_user": "admin",
            "pool_name": topology.get_mgmt_pool_name(),
            "sw_image": self.SRL_IMAGE.format(version=self.version),
            "sw_image_md5": self.SRL_IMAGE_MD5.format(version=self.version),
        }

        return helpers.render_template("node-profile.j2", data)

    def bootstrap_config(self):
        """
        Pushes the bootstrap configuration to the node

        Returns
        -------
        the rendered bootstrap config
        """
        logger.info(f"Pushing bootstrap config to node {self}")
        ssh = SSHClient()
        ssh.set_missing_host_key_policy(AutoAddPolicy())

        data = {"gnmi_port": self.GNMI_PORT}

        bootstrap_config = helpers.render_template("srlinux-bootstrap-config.j2", data)
        fd, path = tempfile.mkstemp()

        try:
            with os.fdopen(fd, "w") as cfg:
                cfg.write(bootstrap_config)
                cfg.flush()
                print(path)
                ssh.connect(
                    self.mgmt_ipv4,
                    username=self.SRL_USERNAME,
                    password=self.SRL_PASSWORD,
                )
                sftp = ssh.open_sftp()
                logger.info("Copying rendered bootstrap-config to node")
                sftp.put(path, "bootstrap-config.cfg")
                logger.info("Sourcing the bootstrap-config file")
                ssh_stdin, ssh_stdout, ssh_stderr = ssh.exec_command(
                    "source bootstrap-config.cfg"
                )
                stderr_lines = ssh_stderr.readlines()
                if len(stderr_lines) > 0:
                    logger.error("=== STDERR ===")
                    logger.error(ssh_stderr)
                    raise Exception(
                        "Something went wrong when pushing bootstrap config to the node, see error above"
                    )
        except (
            BadHostKeyException,
            AuthenticationException,
            SSHException,
            socket.error,
        ) as e:
            logger.critical(f"Could not connect to node {self}, exception: {e}")
            raise e
        finally:
            os.remove(path)

    def get_bootstrap_node(self, topology):
        """
        Creates a bootstrap node for this node

        Returns
        -------
        the rendered bootstrap-node jinja template
        """
        logger.info(f"Creating bootstrap node for {self}")

        role_value = "leaf"
        if "leaf" in self.name:
            role_value = "leaf"
        elif "spine" in self.name:
            role_value = "spine"
        elif "borderleaf" in self.name or "bl" in self.name:
            role_value = "borderleaf"
        elif "dcgw" in self.name:
            role_value = "dcgw"
        else:
            logger.warning(f"Could not determine role of node {self}, defaulting to eda.nokia.com/role=leaf")

        data = {
            "node_name": self.get_node_name(topology),
            "topology_name": topology.get_eda_safe_name(),
            "role_value": role_value,
            "node_profile": self.get_profile_name(topology),
            "kind": self.kind,
            "platform": self.get_platform(),
            "sw_version": self.version,
            "mgmt_ip": self.mgmt_ipv4,
            "system_interface": self.get_system_interface_name(topology),
        }

        return helpers.render_template("bootstrap-node.j2", data)

    def get_system_interface_name(self, topology):
        """
        Returns the name of this node's system interface
        """
        return f"{self.get_node_name(topology)}-system0"

    def get_system_interface(self, topology):
        """
        Creates a system interface for this node

        Parameters
        ----------
        topology: the parsed Topology

        Returns
        -------
        The rendered interface jinja template
        """
        logger.info(f"Creating system interface for {self}")

        data = {
            "interface_name": self.get_system_interface_name(topology),
            "label_key": None,
            "label_value": None,
            "encap_type": "'null'",
            "node_name": self.get_node_name(topology),
            "interface": "system0",
            "description": "system interface",
        }

        return helpers.render_template("interface.j2", data)

    def get_topolink_interface_name(self, topology, ifname):
        """
        Returns the name of this node's topolink with given interface
        """
        return (
            f"{self.get_node_name(topology)}-{self.get_interface_name_for_kind(ifname)}"
        )

    def get_topolink_interface(self, topology, ifname, other_node):
        """
        Creates a topolink interface for this node and interface

        Parameters
        ----------
        topology:   the parsed Topology
        ifname:     name of the topolink interface
        other_node: node at the other end of the topolink (used for description)

        Returns
        -------
        The rendered interface jinja template
        """
        logger.info(f"Creating topolink interface for {self}")

        data = {
            "interface_name": self.get_topolink_interface_name(topology, ifname),
            "label_key": "eda.nokia.com/role",
            "label_value": "interSwitch",
            "encap_type": "'null'",
            "node_name": self.get_node_name(topology),
            "interface": self.get_interface_name_for_kind(ifname),
            "description": f"inter-switch link to {other_node.get_node_name(topology)}",
        }

        return helpers.render_template("interface.j2", data)
