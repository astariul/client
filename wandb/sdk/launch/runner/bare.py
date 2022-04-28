import os
import logging
import shlex
from typing import Any, List, Optional

import wandb

from .abstract import AbstractRun, AbstractRunner
from .local import _run_entry_point
from .._project_spec import get_entry_point_command, LaunchProject
from ..builder.build import get_env_vars_dict
from ..utils import (
    PROJECT_SYNCHRONOUS,
    sanitize_wandb_api_key,
    validate_wandb_python_deps,
    parse_wandb_uri,
)


_logger = logging.getLogger(__name__)


class BareRunner(AbstractRunner):
    """Runner class, uses a project to create a LocallySubmittedRun.

    BareRunner is very similar to a LocalRunner, except it does not
    run the command inside a docker container. Instead, it runs the
    command specified directly on the BARE machine.

    """

    def run(
        self,
        launch_project: LaunchProject,
        *args,
        **kwargs,
    ) -> Optional[AbstractRun]:
        if args is not None:
            _logger.warning(f"BareRunner.run received unused args {args}")
        if kwargs is not None:
            _logger.warning(f"BareRunner.run received unused kwargs {kwargs}")

        synchronous: bool = self.backend_config[PROJECT_SYNCHRONOUS]
        entry_point = launch_project.get_single_entry_point()

        cmd: List[Any] = []

        # Check to make sure local python dependencies match run's requirement.txt
        _, _, run_name = parse_wandb_uri(self.uri)
        validate_wandb_python_deps(
            launch_project.target_entity,
            launch_project.target_project,
            run_name,
            self._api,
            launch_project.project_dir,
        )

        env_vars = get_env_vars_dict(launch_project, self._api)
        for env_key, env_value in env_vars.items():
            cmd += ["-e", f"{shlex.quote(env_key)}={shlex.quote(env_value)}"]

        if not self.ack_run_queue_item(launch_project):
            return None

        entry_cmd = get_entry_point_command(entry_point, launch_project.override_args)
        cmd += [entry_cmd]

        command_str = " ".join(cmd).strip()
        wandb.termlog(
            "Launching run on bare machine with command: {}".format(
                sanitize_wandb_api_key(command_str)
            )
        )
        run = _run_entry_point(command_str, launch_project.project_dir)
        if synchronous:
            run.wait()
        return run
