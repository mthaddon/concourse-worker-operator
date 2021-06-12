#!/usr/bin/env python3
# Copyright 2021 Canonincal Ltd.
# See LICENSE file for licensing details.

import logging
import os
import subprocess

from tempfile import NamedTemporaryFile

from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus
from ops.pebble import ConnectionError

logger = logging.getLogger(__name__)


class ConcourseWorkerOperatorCharm(CharmBase):
    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        self.framework.observe(self.on.config_changed, self._on_config_changed)

        self._stored.set_default(
            concourse_web_host=None,
            concourse_tsa_host_key_pub=None,
        )

        self.framework.observe(self.on.concourse_worker_relation_changed, self._on_concourse_worker_relation_changed)

    def _on_concourse_worker_relation_changed(self, event):
        if not os.path.exists("/concourse-keys/worker_key.pub"):
            logger.info("We don't have /concourse-keys/worker_key.pub to publish on the relation yet, deferring.")
            event.defer()
            return
        # Publish our public key on the relation.
        with open("/concourse-keys/worker_key.pub", "r") as worker_key_pub:
            logger.info("Publishing WORKER_KEY_PUB on concourse-worker relation.")
            event.relation.data[self.unit]["WORKER_KEY_PUB"] = worker_key_pub.read()
            container = self.unit.get_container("concourse-worker")
        tsa_host = event.relation.data[event.app].get("TSA_HOST")
        tsa_host_key_pub = event.relation.data[event.app].get("CONCOURSE_TSA_HOST_KEY_PUB")
        if not tsa_host or not tsa_host_key_pub:
            event.defer()
            return
        try:
            container.push("/concourse-keys/tsa_host_key.pub", tsa_host_key_pub, make_dirs=True)
            self._stored.concourse_web_host = tsa_host
        except ConnectionError:
            logger.info("Unable to push to the container, deferring.")
            event.defer()
            return

    def _get_concourse_binary_path(self):
        container = self.unit.get_container("concourse-worker")
        with NamedTemporaryFile(delete=False) as temp:
            temp.write(container.pull("/usr/local/concourse/bin/concourse", encoding=None).read())
            temp.flush()
            logger.info("Wrote concourse binary to %s", temp.name)

            # Make it executable
            os.chmod(temp.name, 0o777)
        return temp.name

    def _on_config_changed(self, event):
        # Let's check with have the worker key already. If not, let's create it.
        if not os.path.exists("/concourse-keys/worker_key"):
            try:
                concourse_binary_path = self._get_concourse_binary_path()
            except ConnectionError:
                event.defer()
                return
            subprocess.run([concourse_binary_path, "generate-key", "-t", "ssh", "-f", "/concourse-keys/worker_key"])

        if not self._stored.concourse_web_host:
            self.unit.status = BlockedStatus("Relation required with Concourse Web.")
            return

        # Check we have other needed file from relation.
        if not os.path.exists(self._env_config["CONCOURSE_TSA_PUBLIC_KEY"]):
            self.unit.BlockedStatus("Waiting for CONCOURSE_TSA_PUBLIC_KEY")
            event.defer()
            return

        container = self.unit.get_container("concourse-worker")
        layer = self._concourse_layer()
        try:
            services = container.get_plan().to_dict().get("services", {})
        except ConnectionError:
            logger.info("Unable to connect to Pebble, deferring event")
            event.defer()
            return
        if services != layer["services"]:
            container.add_layer("concourse-worker", layer, combine=True)
            logger.info("Added updated layer to concourse")
            if container.get_service("concourse-worker").is_running():
                container.stop("concourse-worker")
            container.start("concourse-worker")
            logger.info("Restarted concourse-worker service")
        self.unit.status = ActiveStatus()

    def _concourse_layer(self):
        return {
            "services": {
                "concourse-worker": {
                    "override": "replace",
                    "summary": "concourse worker node",
                    "command": "/usr/local/bin/entrypoint.sh worker",
                    "startup": "enabled",
                    "environment": self._env_config,
                }
            },
        }

    @property
    def _env_config(self):
        return {
            "CONCOURSE_WORK_DIR": "/opt/concourse/worker",
            "CONCOURSE_TSA_HOST": "{}:2222".format(self._stored.concourse_web_host),  # comma-separated list.
            "CONCOURSE_TSA_PUBLIC_KEY": "/concourse-keys/tsa_host_key.pub",
            "CONCOURSE_TSA_WORKER_PRIVATE_KEY": "/concourse-keys/worker_key",
        }


if __name__ == "__main__":
    main(ConcourseWorkerOperatorCharm, use_juju_for_storage=True)
