"""Installation-time diagnostics; unavailable observations never become evidence.

Reports are transient. They neither rewrite the deployment truth on idle transitions
nor grant a function verification grade. Planner enforcement is a separate step.
"""
import json
import subprocess


def collect_installation_observations(manager, *, services, runtime_spec=None):
    report = {}
    if "embedding-executor" in services:
        try:
            response = manager._compose("production", "exec", "-T", "app", "python", "manage.py",
                                        "observe_embedding_execution", "--timeout", "30")
            if response.returncode:
                raise ValueError("observer command failed")
            observations = json.loads(response.stdout)
            if (not isinstance(observations, dict) or not observations
                    or not all(isinstance(identity, str) and identity.startswith("embedding:")
                               and isinstance(functions, dict) and isinstance(functions.get("text.embed"), dict)
                               for identity, functions in observations.items())):
                raise ValueError("invalid observer report")
            report["embedding"] = {"status": "execution_observed", "observations": observations}
        except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
            # Do not expose command stderr, tokens or endpoint settings.
            report["embedding"] = {"status": "unavailable", "reason": "embedding_observation_unavailable"}
    if runtime_spec is not None:
        try:
            report["llm"] = manager.observe_backend(runtime_spec)
        except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
            report["llm"] = {"status": "unavailable", "reason": "backend_observation_failed"}
    return report


def observation_summary(report):
    """Safe operator output: status/version only, never raw process output."""
    return {role: {key: value for key, value in entry.items()
                   if key in {"status", "reason", "backend_version", "request_settings_observed"}}
            for role, entry in report.items()}
