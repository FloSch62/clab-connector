# clab_connector/services/integration/topology_integrator.py (updated)

import logging

from clab_connector.utils.constants import SUBSTEP_INDENT

from clab_connector.models.topology import parse_topology_file
from clab_connector.clients.eda.client import EDAClient
from clab_connector.clients.kubernetes.client import (
    apply_manifest,
    edactl_namespace_bootstrap,
    wait_for_namespace,
    update_namespace_description,
)
from clab_connector.utils import helpers
from clab_connector.utils.exceptions import EDAConnectionError, ClabConnectorError
from clab_connector.services.integration.sros_post_integration import prepare_sros_node

logger = logging.getLogger(__name__)


class TopologyIntegrator:
    """
    Handles creation of EDA resources for a given containerlab topology.

    Parameters
    ----------
    eda_client : EDAClient
        A connected EDAClient used to submit resources to the EDA cluster.
    """

    def __init__(self, eda_client: EDAClient):
        self.eda_client = eda_client
        self.topology = None

    def run(
        self,
        topology_file,
        eda_url,
        eda_user,
        eda_password,
        verify,
        skip_edge_intfs: bool = False,
    ):
        """
        Parse the topology, run connectivity checks, and create EDA resources.

        Parameters
        ----------
        topology_file : str or Path
            Path to the containerlab topology JSON file.
        eda_url : str
            EDA hostname/IP.
        eda_user : str
            EDA username.
        eda_password : str
            EDA password.
        verify : bool
            Certificate verification flag.
        skip_edge_intfs : bool
            When True, omit edge link resources and their interfaces from the
            integration.

        Returns
        -------
        None

        Raises
        ------
        EDAConnectionError
            If EDA is unreachable or credentials are invalid.
        ClabConnectorError
            If any resource fails validation.
        """
        logger.info("Parsing topology for integration")
        self.topology = parse_topology_file(str(topology_file))

        logger.info("== Running pre-checks ==")
        self.prechecks()

        # Verify connectivity to each node's management interface
        self.topology.check_connectivity()

        logger.info("== Creating namespace ==")
        self.create_namespace()

        logger.info("== Creating artifacts ==")
        self.create_artifacts()

        logger.info("== Creating init ==")
        self.create_init()
        self.eda_client.commit_transaction("create init (bootstrap)")

        logger.info("== Creating node security profile ==")
        self.create_node_security_profile()

        logger.info("== Creating node users ==")
        self.create_node_user_groups()
        self.create_node_users()
        self.eda_client.commit_transaction("create node users and groups")

        logger.info("== Creating node profiles ==")
        self.create_node_profiles()
        self.eda_client.commit_transaction("create node profiles")

        logger.info("== Onboarding nodes ==")
        self.create_toponodes()
        self.eda_client.commit_transaction("create nodes")

        logger.info("== Adding topolink interfaces ==")
        self.create_topolink_interfaces(skip_edge_intfs)
        # Only commit if there are transactions
        if self.eda_client.transactions:
            self.eda_client.commit_transaction("create topolink interfaces")
        else:
            logger.info(f"{SUBSTEP_INDENT}No topolink interfaces to create, skipping.")

        logger.info("== Creating topolinks ==")
        self.create_topolinks(skip_edge_intfs)
        # Only commit if there are transactions
        if self.eda_client.transactions:
            self.eda_client.commit_transaction("create topolinks")
        else:
            logger.info(f"{SUBSTEP_INDENT}No topolinks to create, skipping.")

        logger.info("== Running post-integration steps ==")
        self.run_post_integration()

        logger.info("Done!")

    def prechecks(self):
        """
        Verify that EDA is up and credentials are valid.

        Raises
        ------
        EDAConnectionError
            If EDA is not reachable or not authenticated.
        """
        if not self.eda_client.is_up():
            raise EDAConnectionError("EDA not up or unreachable")
        if not self.eda_client.is_authenticated():
            raise EDAConnectionError("EDA credentials invalid")

    def create_namespace(self):
        """
        Create and bootstrap a namespace for the topology in EDA.
        """
        ns = f"clab-{self.topology.name}"
        try:
            edactl_namespace_bootstrap(ns)
            wait_for_namespace(ns)
            desc = f"Containerlab {self.topology.name}: {self.topology.clab_file_path}"
            success = update_namespace_description(ns, desc)
            if not success:
                logger.warning(
                    f"{SUBSTEP_INDENT}Created namespace '{ns}' but could not update its description. Continuing with integration."
                )
        except Exception as e:
            # If namespace creation itself fails, we should stop the process
            logger.error(f"Failed to create namespace '{ns}': {e}")
            raise

    def create_artifacts(self):
        """
        Create Artifact resources for nodes that need them.

        Skips creation if already exists or no artifact data is available.
        """
        logger.info(f"{SUBSTEP_INDENT}Creating artifacts for nodes that need them")
        nodes_by_artifact = {}
        for node in self.topology.nodes:
            if not node.needs_artifact():
                continue
            artifact_name, filename, download_url = node.get_artifact_info()
            if not artifact_name or not filename or not download_url:
                logger.warning(
                    f"{SUBSTEP_INDENT}No artifact info for node {node.name}; skipping."
                )
                continue
            if artifact_name not in nodes_by_artifact:
                nodes_by_artifact[artifact_name] = {
                    "nodes": [],
                    "filename": filename,
                    "download_url": download_url,
                    "version": node.version,
                }
            nodes_by_artifact[artifact_name]["nodes"].append(node.name)

        for artifact_name, info in nodes_by_artifact.items():
            first_node = info["nodes"][0]
            logger.info(
                f"{SUBSTEP_INDENT}Creating YANG artifact for node: {first_node} (version={info['version']})"
            )
            artifact_yaml = self.topology.nodes[0].get_artifact_yaml(
                artifact_name, info["filename"], info["download_url"]
            )
            if not artifact_yaml:
                logger.warning(
                    f"{SUBSTEP_INDENT}Could not generate artifact YAML for {first_node}"
                )
                continue
            try:
                apply_manifest(artifact_yaml, namespace="eda-system")
                logger.info(f"{SUBSTEP_INDENT}Artifact '{artifact_name}' created.")
                other_nodes = info["nodes"][1:]
                if other_nodes:
                    logger.info(
                        f"{SUBSTEP_INDENT}Using same artifact for nodes: {', '.join(other_nodes)}"
                    )
            except RuntimeError as ex:
                if "AlreadyExists" in str(ex):
                    logger.info(f"{SUBSTEP_INDENT}Artifact '{artifact_name}' already exists.")
                else:
                    logger.error(f"Error creating artifact '{artifact_name}': {ex}")

    def create_init(self):
        """
        Create an Init resource in the namespace to bootstrap additional resources.
        """
        data = {"namespace": f"clab-{self.topology.name}"}
        yml = helpers.render_template("init.yaml.j2", data)
        item = self.eda_client.add_replace_to_transaction(yml)
        if not self.eda_client.is_transaction_item_valid(item):
            raise ClabConnectorError("Validation error for init resource")

    def create_node_security_profile(self):
        """
        Create a NodeSecurityProfile resource that references an EDA node issuer.
        """
        data = {"namespace": f"clab-{self.topology.name}"}
        yaml_str = helpers.render_template("nodesecurityprofile.yaml.j2", data)
        try:
            apply_manifest(yaml_str, namespace=f"clab-{self.topology.name}")
            logger.info(f"{SUBSTEP_INDENT}Node security profile created.")
        except RuntimeError as ex:
            if "AlreadyExists" in str(ex):
                logger.info(f"{SUBSTEP_INDENT}Node security profile already exists, skipping.")
            else:
                raise

    def create_node_user_groups(self):
        """
        Create a NodeGroup resource for user groups (like 'sudo').
        """
        data = {"namespace": f"clab-{self.topology.name}"}
        node_user_group = helpers.render_template("node-user-group.yaml.j2", data)
        item = self.eda_client.add_replace_to_transaction(node_user_group)
        if not self.eda_client.is_transaction_item_valid(item):
            raise ClabConnectorError("Validation error for node user group")

    def create_node_users(self):
        """
        Create NodeUser resources with SSH pub keys for SRL and SROS nodes.
        """
        ssh_pub_keys = getattr(self.topology, "ssh_pub_keys", [])
        if not ssh_pub_keys:
            logger.warning(
                f"{SUBSTEP_INDENT}No SSH public keys found. Proceeding with an empty key list."
            )

        # Create SRL node user
        srl_data = {
            "namespace": f"clab-{self.topology.name}",
            "node_user": "admin",
            "username": "admin",
            "password": "NokiaSrl1!",
            "ssh_pub_keys": ssh_pub_keys,
            "node_selector": "containerlab=managedSrl"
        }
        srl_node_user = helpers.render_template("node-user.j2", srl_data)
        item_srl = self.eda_client.add_replace_to_transaction(srl_node_user)
        if not self.eda_client.is_transaction_item_valid(item_srl):
            raise ClabConnectorError("Validation error for SRL node user")

        # Create SROS node user
        sros_data = {
            "namespace": f"clab-{self.topology.name}",
            "node_user": "admin-sros",
            "username": "admin",
            "password": "NokiaSros1!",
            "ssh_pub_keys": ssh_pub_keys,
            "node_selector": "containerlab=managedSros"
        }
        sros_node_user = helpers.render_template("node-user.j2", sros_data)
        item_sros = self.eda_client.add_replace_to_transaction(sros_node_user)
        if not self.eda_client.is_transaction_item_valid(item_sros):
            raise ClabConnectorError("Validation error for SROS node user")

    def create_node_profiles(self):
        """
        Create NodeProfile resources for each EDA-supported node version-kind combo.
        """
        profiles = self.topology.get_node_profiles()
        for prof_yaml in profiles:
            item = self.eda_client.add_replace_to_transaction(prof_yaml)
            if not self.eda_client.is_transaction_item_valid(item):
                raise ClabConnectorError("Validation error creating node profile")

    def create_toponodes(self):
        """
        Create TopoNode resources for each node.
        """
        tnodes = self.topology.get_toponodes()
        for node_yaml in tnodes:
            item = self.eda_client.add_replace_to_transaction(node_yaml)
            if not self.eda_client.is_transaction_item_valid(item):
                raise ClabConnectorError("Validation error creating toponode")

    def create_topolink_interfaces(self, skip_edge_intfs: bool = False):
        """
        Create Interface resources for each link endpoint in the topology.
        """
        interfaces = self.topology.get_topolink_interfaces(
            skip_edge_link_interfaces=skip_edge_intfs
        )
        for intf_yaml in interfaces:
            item = self.eda_client.add_replace_to_transaction(intf_yaml)
            if not self.eda_client.is_transaction_item_valid(item):
                raise ClabConnectorError("Validation error creating topolink interface")

    def create_topolinks(self, skip_edge_links: bool = False):
        """Create TopoLink resources for each EDA-supported link in the topology.

        Parameters
        ----------
        skip_edge_links : bool, optional
            When True, omit TopoLink resources for edge links. Defaults to False.
        """
        links = self.topology.get_topolinks(skip_edge_links=skip_edge_links)
        for l_yaml in links:
            item = self.eda_client.add_replace_to_transaction(l_yaml)
            if not self.eda_client.is_transaction_item_valid(item):
                raise ClabConnectorError("Validation error creating topolink")

    def run_post_integration(self):
        """
        Run any post-integration steps required for specific node types.
        """
        namespace = f"clab-{self.topology.name}"
        # Determine if we should be quiet based on the current log level
        quiet = logging.getLogger().getEffectiveLevel() > logging.INFO

        # Look for SROS nodes and run post-integration for them
        for node in self.topology.nodes:
            if node.kind == "nokia_sros":
                logger.info(f"{SUBSTEP_INDENT}Running SROS post-integration for node {node.name}")
                try:
                    # Get normalized version from the node
                    normalized_version = node._normalize_version(node.version)
                    success = prepare_sros_node(
                        node_name=node.get_node_name(self.topology),
                        namespace=namespace,
                        version=normalized_version,
                        mgmt_ip=node.mgmt_ipv4,
                        username="admin",
                        password="admin",
                        quiet=quiet  # Pass quiet parameter
                    )
                    if success:
                        logger.info(f"{SUBSTEP_INDENT}SROS post-integration for {node.name} completed successfully")
                    else:
                        logger.error(f"SROS post-integration for {node.name} failed")
                except Exception as e:
                    logger.error(f"Error during SROS post-integration for {node.name}: {e}")