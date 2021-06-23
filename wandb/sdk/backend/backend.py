#
# -*- coding: utf-8 -*-
"""Backend - Send to internal process

Manage backend.

"""

import importlib
import logging
import multiprocessing
import os
import subprocess
import sys
import threading
import time

import wandb

from ..interface import interface
from ..internal.internal import wandb_internal

if wandb.TYPE_CHECKING:  # type: ignore
    from typing import Any, Dict, Optional

logger = logging.getLogger("wandb")


class BackendThread(threading.Thread):
    """Class to running internal process as a thread."""

    def __init__(self, target, kwargs) -> None:
        threading.Thread.__init__(self)
        self._target = target
        self._kwargs = kwargs
        self.daemon = True
        self.pid = 0

    def run(self):
        self._target(**self._kwargs)


class Backend(object):
    # multiprocessing context or module
    _multiprocessing: object
    # interface: interface.BackendSenderBase

    def __init__(self, settings=None, log_level=None):
        self._done = False
        self.record_q = None
        self.result_q = None
        self.wandb_process = None
        self.interface = None
        self._internal_pid = None
        self._settings = settings
        self._log_level = log_level

        self._multiprocessing = multiprocessing
        self._multiprocessing_setup()

        # for _module_main_* methods
        self._save_mod_name = None
        self._save_mod_path = None
        self._save_mod_spec = None

    def _hack_set_run(self, run):
        self.interface._hack_set_run(run)

    def _multiprocessing_setup(self):
        if self._settings.start_method in {"thread", "grpc"}:
            return

        # defaulting to spawn for now, fork needs more testing
        start_method = self._settings.start_method or "spawn"

        # TODO: use fork context if unix and frozen?
        # if py34+, else fall back
        if not hasattr(multiprocessing, "get_context"):
            return
        all_methods = multiprocessing.get_all_start_methods()
        logger.info(
            "multiprocessing start_methods={}, using: {}".format(
                ",".join(all_methods), start_method
            )
        )
        ctx = multiprocessing.get_context(start_method)
        self._multiprocessing = ctx

    def _grpc_wait_for_port(self, fname, proc=None) -> Optional[int]:
        time_max = time.time() + 30
        port = None
        while time.time() < time_max:
            if proc and proc.poll():
                # process finished
                print("proc exited with", proc.returncode)
                return None
            if not os.path.isfile(fname):
                time.sleep(0.2)
                continue
            try:
                f = open(fname)
                port = int(f.read())
            except Exception as e:
                print("Error:", e)
            return port
        return None

    def _grpc_launch_server(self, port=0) -> Optional[int]:
        # https://github.com/wandb/client/blob/archive/old-cli/wandb/__init__.py
        # https://stackoverflow.com/questions/1196074/how-to-start-a-background-process-in-python
        kwargs: Dict[str, Any] = dict(close_fds=True, start_new_session=True)

        # TODO(add processid)
        pid = os.getpid()
        fname = "/tmp/out-{}-port.txt".format(pid)

        try:
            os.unlink(fname)
        except Exception:
            pass

        pid_str = str(os.getpid())
        internal_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "wandb",
                "grpc-server",
                "--port-filename",
                fname,
                "--pid",
                pid_str,
                # "--debug",
                # "true",
            ],
            env=os.environ,
            **kwargs
        )

        port = self._grpc_wait_for_port(fname, proc=internal_proc)
        try:
            os.unlink(fname)
        except Exception:
            pass

        return port

    def _module_main_install(self):
        # Support running code without a: __name__ == "__main__"
        main_module = sys.modules["__main__"]
        main_mod_spec = getattr(main_module, "__spec__", None)
        main_mod_path = getattr(main_module, "__file__", None)
        main_mod_name = None
        if main_mod_spec is None:  # hack for pdb
            main_mod_spec = (
                importlib.machinery.ModuleSpec(
                    name="wandb.mpmain", loader=importlib.machinery.BuiltinImporter
                )
                if sys.version_info[0] > 2
                else None
            )
            main_module.__spec__ = main_mod_spec
        else:
            self._save_mod_spec = main_mod_spec

        if main_mod_name is not None:
            self._save_mod_name = main_mod_name
            main_module.__spec__.name = "wandb.mpmain"
        elif main_mod_path is not None:
            self._save_mod_path = main_module.__file__
            fname = os.path.join(
                os.path.dirname(wandb.__file__), "mpmain", "__main__.py"
            )
            main_module.__file__ = fname

    def _module_main_uninstall(self):
        main_module = sys.modules["__main__"]
        # Undo temporary changes from: __name__ == "__main__"
        main_module.__spec__ = self._save_mod_spec
        if self._save_mod_name:
            main_module.__spec__.name = self._save_mod_name
        elif self._save_mod_path:
            main_module.__file__ = self._save_mod_path

    def _ensure_launched_grpc(self):
        from ..interface import iface_grpc

        grpc_port = self._grpc_launch_server()
        self.interface = iface_grpc.BackendGrpcSender()
        self.interface._connect(grpc_port)

    def ensure_launched(self):
        """Launch backend worker if not running."""
        settings = dict(self._settings or ())
        settings["_log_level"] = self._log_level or logging.DEBUG

        # TODO: this is brittle and should likely be handled directly on the
        # settings object.  Multi-processing blows up when it can't pickle
        # objects.
        if "_early_logger" in settings:
            del settings["_early_logger"]

        start_method = settings.get("start_method")

        if start_method == "grpc":
            self._ensure_launched_grpc()
            return

        self.record_q = self._multiprocessing.Queue()
        self.result_q = self._multiprocessing.Queue()
        user_pid = os.getpid()

        if start_method == "thread":
            wandb._set_internal_process(disable=True)
            self.wandb_process = BackendThread(
                target=wandb_internal,
                kwargs=dict(
                    settings=settings,
                    record_q=self.record_q,
                    result_q=self.result_q,
                    user_pid=user_pid,
                ),
            )
        else:
            self.wandb_process = self._multiprocessing.Process(
                target=wandb_internal,
                kwargs=dict(
                    settings=settings,
                    record_q=self.record_q,
                    result_q=self.result_q,
                    user_pid=user_pid,
                ),
            )
            self.wandb_process.name = "wandb_internal"

        self._module_main_install()

        logger.info("starting backend process...")
        # Start the process with __name__ == "__main__" workarounds
        assert self.wandb_process
        self.wandb_process.start()
        self._internal_pid = self.wandb_process.pid
        logger.info(
            "started backend process with pid: {}".format(self.wandb_process.pid)
        )

        self._module_main_uninstall()

        self.interface = interface.BackendSender(
            process=self.wandb_process, record_q=self.record_q, result_q=self.result_q,
        )

    def server_connect(self):
        """Connect to server."""
        pass

    def server_status(self):
        """Report server status."""
        pass

    def cleanup(self):
        # TODO: make _done atomic
        if self._done:
            return
        self._done = True
        self.interface.join()
        if self.wandb_process:
            self.wandb_process.join()

        if self.record_q:
            self.record_q.close()
        if self.result_q:
            self.result_q.close()
        # No printing allowed from here until redirect restore!!!
