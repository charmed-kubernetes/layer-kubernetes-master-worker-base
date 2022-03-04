from subprocess import call
import time

from charms.layer.kubernetes_common import get_node_name
from charms.reactive import is_state
from charmhelpers.core import hookenv, unitdata


db = unitdata.kv()


class LabelMaker:
    class NodeLabelError(Exception):
        pass

    def __init__(self, kubeconfig):
        self.kubeconfig = kubeconfig
        self.node = get_node_name()

    @staticmethod
    def _persistent_call(cmd, retry_message):
        deadline = time.time() + 180
        while time.time() < deadline:
            code = call(cmd)
            if code == 0:
                return True
            hookenv.log(retry_message)
            time.sleep(1)
        else:
            return False

    def set_label(self, label, value):
        cmd = "kubectl --kubeconfig={0} label node {1} {2}={3} --overwrite"
        cmd = cmd.format(self.kubeconfig, self.node, label, value)
        cmd = cmd.split()
        retry = "Failed to apply label %s=%s. Will retry." % (label, value)
        if not self._persistent_call(cmd, retry):
            raise self.NodeLabelError(retry)

    def remove_label(self, label):
        cmd = "kubectl --kubeconfig={0} label node {1} {2}-"
        cmd = cmd.format(self.kubeconfig, self.node, label)
        cmd = cmd.split()
        retry = "Failed to remove label {0}. Will retry.".format(label)
        if not self._persistent_call(cmd, retry):
            raise self.NodeLabelError(retry)

    def apply_node_labels(self):
        """
        Parse the `labels` configuration option and apply the labels to the
        node.
        """
        # Get the user's configured labels.
        config = hookenv.config()
        user_labels = {}
        for item in config.get("labels").split(" "):
            if "=" in item:
                key, val = item.split("=")
                user_labels[key] = val
            else:
                hookenv.log("Skipping malformed option: {}.".format(item))
        # Collect the current label state.
        current_labels = db.get("current_labels") or {}

        try:
            # Remove any labels that the user has removed from the config.
            for key in list(current_labels.keys()):
                if key not in user_labels:
                    self.remove_label(key)
                    del current_labels[key]
                    db.set("current_labels", current_labels)

            # Add any new labels.
            for key, val in user_labels.items():
                self.set_label(key, val)
                current_labels[key] = val
                db.set("current_labels", current_labels)

            # Set the juju-application label.
            self.set_label("juju-application", hookenv.service_name())

            # Set the juju.io/cloud label.
            if is_state("endpoint.aws.ready"):
                self.set_label("juju.io/cloud", "ec2")
            elif is_state("endpoint.gcp.ready"):
                self.set_label("juju.io/cloud", "gce")
            elif is_state("endpoint.openstack.ready"):
                self.set_label("juju.io/cloud", "openstack")
            elif is_state("endpoint.vsphere.ready"):
                self.set_label("juju.io/cloud", "vsphere")
            elif is_state("endpoint.azure.ready"):
                self.set_label("juju.io/cloud", "azure")
            else:
                self.remove_label("juju.io/cloud")
        except self.NodeLabelError as e:
            hookenv.log(str(e))
            raise
