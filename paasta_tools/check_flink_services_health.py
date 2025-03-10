#!/usr/bin/env python
# Copyright 2015-2019 Yelp Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Usage: ./check_flink_services_health.py [options]
"""
import datetime
import logging
from typing import Sequence
from typing import Tuple

import pysensu_yelp

from paasta_tools import flink_tools
from paasta_tools.check_services_replication_tools import main
from paasta_tools.flink_tools import FlinkDeploymentConfig
from paasta_tools.kubernetes_tools import filter_pods_by_service_instance
from paasta_tools.kubernetes_tools import is_pod_ready
from paasta_tools.kubernetes_tools import V1Pod
from paasta_tools.monitoring_tools import check_under_replication
from paasta_tools.monitoring_tools import send_replication_event
from paasta_tools.smartstack_tools import KubeSmartstackReplicationChecker
from paasta_tools.utils import is_under_replicated


log = logging.getLogger(__name__)


def container_lifetime(pod: V1Pod,) -> datetime.timedelta:
    """Return a time duration for how long the pod is alive
    """
    st = pod.status.start_time
    return datetime.datetime.now(st.tzinfo) - st


def healthy_flink_containers_cnt(si_pods: Sequence[V1Pod], container_type: str) -> int:
    """Return count of healthy Flink containers with given type
    """
    return len(
        [
            pod
            for pod in si_pods
            if pod.metadata.labels["flink-container-type"] == container_type
            and is_pod_ready(pod)
            and container_lifetime(pod).total_seconds() > 60
        ]
    )


def check_under_registered_taskmanagers(
    instance_config: FlinkDeploymentConfig, expected_count: int,
) -> Tuple[bool, str]:
    """Check if not enough taskmanagers have been registered to the jobmanager and
    returns both the result of the check in the form of a boolean and a human-readable
    text to be used in logging or monitoring events.
    """
    unhealthy = True
    try:
        overview = flink_tools.get_flink_jobmanager_overview(
            instance_config.service, instance_config.instance, instance_config.cluster
        )
        num_reported = overview.get("taskmanagers", 0)
        crit_threshold = instance_config.get_replication_crit_percentage()
        output = (
            f"Service {instance_config.job_id} has "
            f"{num_reported} out of {expected_count} expected instances "
            f"of taskmanager reported by dashboard!\n"
            f"(threshold: {crit_threshold}%)"
        )
        unhealthy, _ = is_under_replicated(num_reported, expected_count, crit_threshold)
    except ValueError as e:
        output = (
            f"Dashboard of service {instance_config.job_id} is not available!\n({e})"
        )
    if unhealthy:
        output += f"""
What this alert means:

  This alert means that the Flink dashboard is not reporting the expected
  number of taskmanagers.

Reasons this might be happening:

  The service may simply be unhealthy. There also may not be enough resources
  in the cluster to support the requested instance count.

Things you can do:

  * Fix the cause of the unhealthy service. Try running:

     paasta status -s {instance_config.service} -i {instance_config.instance} -c {instance_config.cluster} -vv

"""
    return (unhealthy, output)


def check_flink_service_health(
    instance_config: FlinkDeploymentConfig,
    all_pods: Sequence[V1Pod],
    smartstack_replication_checker: KubeSmartstackReplicationChecker,
) -> None:
    si_pods = filter_pods_by_service_instance(
        pod_list=all_pods,
        service=instance_config.service,
        instance=instance_config.instance,
    )
    taskmanagers_expected_cnt = instance_config.config_dict.get(
        "taskmanager", {"instances": 10}
    ).get("instances", 10)
    num_healthy_supervisors = healthy_flink_containers_cnt(si_pods, "supervisor")
    num_healthy_jobmanagers = healthy_flink_containers_cnt(si_pods, "jobmanager")
    num_healthy_taskmanagers = healthy_flink_containers_cnt(si_pods, "taskmanager")

    results = [
        check_under_replication(
            instance_config=instance_config,
            expected_count=1,
            num_available=num_healthy_supervisors,
            sub_component="supervisor",
        ),
        check_under_replication(
            instance_config=instance_config,
            expected_count=1,
            num_available=num_healthy_jobmanagers,
            sub_component="jobmanager",
        ),
        check_under_replication(
            instance_config=instance_config,
            expected_count=taskmanagers_expected_cnt,
            num_available=num_healthy_taskmanagers,
            sub_component="taskmanager",
        ),
        check_under_registered_taskmanagers(
            instance_config=instance_config, expected_count=taskmanagers_expected_cnt,
        ),
    ]
    output = "\n########\n".join([r[1] for r in results])
    if any(r[0] for r in results):
        log.error(output)
        status = pysensu_yelp.Status.CRITICAL
    else:
        log.info(output)
        status = pysensu_yelp.Status.OK
    send_replication_event(
        instance_config=instance_config, status=status, output=output
    )


if __name__ == "__main__":
    main(
        flink_tools.FlinkDeploymentConfig,
        check_flink_service_health,
        namespace="paasta-flinks",
    )
