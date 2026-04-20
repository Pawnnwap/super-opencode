from __future__ import annotations

import logging
import platform
import subprocess

logger = logging.getLogger(__name__)


def kill_processes_by_keywords(keywords: list[str]) -> None:
    try:
        system = platform.system()
        if system == "Windows":
            try:
                result = subprocess.run(
                    ["tasklist", "/fo", "csv"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                found_processes = False
                for line in result.stdout.splitlines()[1:]:
                    if not line.strip():
                        continue
                    parts = line.split('","')
                    if len(parts) < 2:
                        continue
                    process_name = parts[0].strip('"').lower()
                    if any(keyword in process_name for keyword in keywords):
                        pid = parts[1].strip('"')
                        try:
                            subprocess.run(
                                ["taskkill", "/PID", pid, "/F"],
                                capture_output=True,
                                text=True,
                                timeout=2,
                            )
                            found_processes = True
                        except Exception as exc:
                            logger.warning("Error killing process %s: %s", pid, exc)
                if not found_processes:
                    logger.debug("No matching processes found to kill")
                return
            except subprocess.TimeoutExpired:
                logger.debug("Primary process scan timed out, using fallback method")
            except Exception as exc:
                logger.warning("Primary process scan failed: %s", exc)

            try:
                result = subprocess.run(
                    ["tasklist"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                found_processes = False
                for line in result.stdout.splitlines()[3:]:
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    process_name = parts[0].lower()
                    if any(keyword in process_name for keyword in keywords):
                        pid = parts[1]
                        try:
                            subprocess.run(
                                ["taskkill", "/PID", pid, "/F"],
                                capture_output=True,
                                text=True,
                                timeout=2,
                            )
                            found_processes = True
                        except Exception as exc:
                            logger.warning("Error killing process %s: %s", pid, exc)
                if not found_processes:
                    logger.debug("No matching processes found to kill (fallback)")
            except Exception as exc:
                logger.warning("Fallback process scan failed: %s", exc)
            return

        try:
            subprocess.run(
                ["pkill", "-f", "|".join(keywords)],
                capture_output=True,
                text=True,
                timeout=2,
            )
        except FileNotFoundError:
            logger.debug("pkill not available on this system")
        except subprocess.TimeoutExpired:
            logger.debug("Unix pkill command timed out")
        except Exception as exc:
            logger.warning("Unix pkill failed: %s", exc)
    except Exception as exc:
        logger.warning("Error while killing processes: %s", exc)
