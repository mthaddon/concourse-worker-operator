#!/usr/bin/env python3
# Copyright 2021 Canonincal Ltd.
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charm the service.

Refer to the following post for a quick-start guide that will help you
develop a new k8s charm using the Operator Framework:

    https://discourse.charmhub.io/t/4208
"""

import logging
import ops.lib

from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus
from ops.pebble import ConnectionError

pgsql = ops.lib.use("pgsql", 1, "postgresql-charmers@lists.launchpad.net")

logger = logging.getLogger(__name__)


class ConcourseWorkerOperatorCharm(CharmBase):
    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        self.framework.observe(self.on.config_changed, self._on_config_changed)

        self._stored.set_default(
            db_name=None,
            db_host=None,
            db_port=None,
            db_user=None,
            db_password=None,
            db_conn_str=None,
            db_uri=None,
            db_ro_uris=[],
            concourse_web_host=None,
            concourse_tsa_host_key_pub=None,
        )
        self.db = pgsql.PostgreSQLClient(self, 'db')
        self.framework.observe(self.db.on.database_relation_joined, self._on_database_relation_joined)
        self.framework.observe(self.db.on.master_changed, self._on_master_changed)
        self.framework.observe(self.db.on.standby_changed, self._on_standby_changed)

        self.framework.observe(self.on.concourse_worker_relation_changed, self._on_concourse_worker_relation_changed)

    def _on_concourse_worker_relation_changed(self, event):
        try:
            container = self.unit.get_container("concourse-worker")
        except ConnectionError:
            event.defer()
            return
        tsa_host = event.relation.data[event.app].get("TSA_HOST")
        tsa_host_key_pub = event.relation.data[event.app].get("CONCOURSE_TSA_HOST_KEY_PUB")
        if not tsa_host or not tsa_host_key_pub:
            event.defer()
            return
        self._stored.concourse_web_host = tsa_host
        container.push("/concourse-keys/tsa_host_key.pub", tsa_host_key_pub, make_dirs=True)

    def _get_concourse_binary_path(self, container):
        with NamedTemporaryFile(delete=False) as temp:
            temp.write(container.pull("/usr/local/concourse/bin/concourse", encoding=None).read())
            temp.flush()
            logger.info("Wrote concourse binary to %s", temp.name)

            # Make it executable
            os.chmod(temp.name, 0o777)
        return temp.name

    def _on_database_relation_joined(self, event: pgsql.DatabaseRelationJoinedEvent):
        if self.model.unit.is_leader():
            # Provide requirements to the PostgreSQL server.
            event.database = 'concourse'
        elif event.database != 'concourse':
            # Leader has not yet set requirements. Defer, incase this unit
            # becomes leader and needs to perform that operation.
            event.defer()
            return

    def _on_master_changed(self, event: pgsql.MasterChangedEvent):
        if event.database != 'concourse':
            # Leader has not yet set requirements. Wait until next event,
            # or risk connecting to an incorrect database.
            return

        # The connection to the primary database has been created,
        # changed or removed. More specific events are available, but
        # most charms will find it easier to just handle the Changed
        # events. event.master is None if the master database is not
        # available, or a pgsql.ConnectionString instance.
        self._stored.db_name = event.database
        self._stored.db_host = event.master.host if event.master else None
        self._stored.db_port = event.master.port if event.master else None
        self._stored.db_user = event.master.user if event.master else None
        self._stored.db_password = event.master.password if event.master else None
        self._stored.db_conn_str = None if event.master is None else event.master.conn_str

        # Trigger our config changed hook again.
        self.on.config_changed.emit()

    def _on_standby_changed(self, event: pgsql.StandbyChangedEvent):
        if event.database != 'concourse':
            # Leader has not yet set requirements. Wait until next event,
            # or risk connecting to an incorrect database.
            return

        # Charms needing access to the hot standby databases can get
        # their connection details here. Applications can scale out
        # horizontally if they can make use of the read only hot
        # standby replica databases, rather than only use the single
        # master. event.stanbys will be an empty list if no hot standby
        # databases are available.
        self._stored.db_ro_uris = [c.uri for c in event.standbys]

    def _on_config_changed(self, event):
        required_relations = []
        # XXX: Is this needed at all?
        # if not self._stored.db_conn_str:
        #    required_relations.append("PostgreSQL")
        if required_relations:
            self.unit.status = BlockedStatus(
                "The following relations are required: {}".format(", ".join(required_relations))
            )
            return
        if not self._stored.concourse_web_host:
            self.unit.status = BlockedStatus("Relation required with Concourse Web.")
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
            "CONCOURSE_TSA_WORKER_PRIVATE_KEY": "/concourse-keys/worker_key",  # XXX: We need to generate this.
            # "CONCOURSE_POSTGRES_HOST": self._stored.db_host,
            # "CONCOURSE_POSTGRES_PORT": self._stored.db_port,
            # "CONCOURSE_POSTGRES_DATABASE": self._stored.db_name,
            # "CONCOURSE_POSTGRES_USER": self._stored.db_user,
            # "CONCOURSE_POSTGRES_PASSWORD": self._stored.db_password,
        }


if __name__ == "__main__":
    main(ConcourseWorkerOperatorCharm, use_juju_for_storage=True)
