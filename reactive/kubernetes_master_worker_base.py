import os
from subprocess import check_call, call
import time

from charms.layer import snap
from charms.leadership import leader_get, leader_set
from charms.reactive import (
    clear_flag,
    data_changed,
    hook,
    is_state,
    set_flag,
    when,
    when_not,
    when_any,
)

from charmhelpers.core import hookenv, unitdata
from charmhelpers.core.host import is_container
from charmhelpers.core.sysctl import create as create_sysctl
from charms.layer.kubernetes_common import arch, get_node_name

db = unitdata.kv()


@hook("upgrade-charm")
def upgrade_charm():
    clear_flag("kubernetes.cni-plugins.installed")


@when_not("kubernetes.cni-plugins.installed")
def install_cni_plugins():
    """Unpack the cni-plugins resource"""
    hookenv.status_set("maintenance", "Installing CNI plugins")

    # Get the resource via resource_get
    try:
        resource_name = "cni-{}".format(arch())
        archive = hookenv.resource_get(resource_name)
    except Exception:
        message = "Error fetching the cni resource."
        hookenv.log(message)
        return

    if not archive:
        hookenv.log("Missing cni resource.")
        return

    # Handle null resource publication, we check if filesize < 1mb
    filesize = os.stat(archive).st_size
    if filesize < 1000000:
        hookenv.log("Incomplete cni resource.")
        return

    unpack_path = "/opt/cni/bin"
    os.makedirs(unpack_path, exist_ok=True)
    cmd = ["tar", "xfvz", archive, "-C", unpack_path]
    hookenv.log(cmd)
    check_call(cmd)

    set_flag("kubernetes.cni-plugins.installed")


@when_any("kubernetes-master.snaps.installed", "kubernetes-worker.snaps.installed")
@when("snap.refresh.set")
@when("leadership.is_leader")
def process_snapd_timer():
    """
    Set the snapd refresh timer on the leader so all cluster members
    (present and future) will refresh near the same time.

    :return: None
    """
    # Get the current snapd refresh timer; we know layer-snap has set this
    # when the 'snap.refresh.set' flag is present.
    timer = snap.get(snapname="core", key="refresh.timer").decode("utf-8").strip()
    if not timer:
        # The core snap timer is empty. This likely means a subordinate timer
        # reset ours. Try to set it back to a previously leader-set value,
        # falling back to config if needed. Luckily, this should only happen
        # during subordinate install, so this should remain stable afterward.
        timer = leader_get("snapd_refresh") or hookenv.config("snapd_refresh")
        snap.set_refresh_timer(timer)

        # Ensure we have the timer known by snapd (it may differ from config).
        timer = snap.get(snapname="core", key="refresh.timer").decode("utf-8").strip()

    # The first time through, data_changed will be true. Subsequent calls
    # should only update leader data if something changed.
    if data_changed("snapd_refresh", timer):
        hookenv.log("setting leader snapd_refresh timer to: {}".format(timer))
        leader_set({"snapd_refresh": timer})


@when_any("kubernetes-master.snaps.installed", "kubernetes-worker.snaps.installed")
@when("snap.refresh.set")
@when("leadership.changed.snapd_refresh")
@when_not("leadership.is_leader")
def set_snapd_timer():
    """
    Set the snapd refresh.timer on non-leader cluster members.

    :return: None
    """
    # NB: This method should only be run when 'snap.refresh.set' is present.
    # Layer-snap will always set a core refresh.timer, which may not be the
    # same as our leader. Gating with 'snap.refresh.set' ensures layer-snap
    # has finished and we are free to set our config to the leader's timer.
    timer = leader_get("snapd_refresh") or ""  # None will error
    hookenv.log("setting snapd_refresh timer to: {}".format(timer))
    snap.set_refresh_timer(timer)


@when("config.changed.sysctl")
def write_sysctl():
    """
    :return: None
    """
    sysctl_settings = hookenv.config("sysctl")
    if sysctl_settings and not is_container():
        create_sysctl(
            sysctl_settings,
            "/etc/sysctl.d/50-kubernetes-charm.conf",
            # Some keys in the config may not exist in /proc/sys/net/.
            # For example, the conntrack module may not be loaded when
            # using lxd drivers insteam of kvm. In these cases, we
            # simply ignore the missing keys, rather than making time
            # consuming calls out to the filesystem to check for their
            # existence.
            ignore=True,
        )


class ApplyNodeLabelError(Exception):
    pass


class LabelMaker:
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

    def _set_label(self, label, value):
        cmd = "kubectl --kubeconfig={0} label node {1} {2}={3} --overwrite"
        cmd = cmd.format(self.kubeconfig, self.node, label, value)
        cmd = cmd.split()
        retry = "Failed to apply label %s=%s. Will retry." % (label, value)
        if not self._persistent_call(cmd, retry):
            raise ApplyNodeLabelError(retry)

    def _remove_label(self, label):
        cmd = "kubectl --kubeconfig={0} label node {1} {2}-"
        cmd = cmd.format(self.kubeconfig, self.node, label)
        cmd = cmd.split()
        retry = "Failed to remove label {0}. Will retry.".format(label)
        if not self._persistent_call(cmd, retry):
            raise ApplyNodeLabelError(retry)

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
                    self._remove_label(key)
                    del current_labels[key]
                    db.set("current_labels", current_labels)

            # Add any new labels.
            for key, val in user_labels.items():
                self._set_label(key, val)
                current_labels[key] = val
                db.set("current_labels", current_labels)

            # Set the juju-application label.
            self._set_label("juju-application", hookenv.service_name())

            # Set the juju.io/cloud label.
            if is_state("endpoint.aws.ready"):
                self._set_label("juju.io/cloud", "ec2")
            elif is_state("endpoint.gcp.ready"):
                self._set_label("juju.io/cloud", "gce")
            elif is_state("endpoint.openstack.ready"):
                self._set_label("juju.io/cloud", "openstack")
            elif is_state("endpoint.vsphere.ready"):
                self._set_label("juju.io/cloud", "vsphere")
            elif is_state("endpoint.azure.ready"):
                self._set_label("juju.io/cloud", "azure")
            else:
                self._remove_label("juju.io/cloud")
        except ApplyNodeLabelError as e:
            hookenv.log(str(e))
            raise
