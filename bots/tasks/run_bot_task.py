import logging
import os
import signal

import logging

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from celery.signals import worker_shutting_down

from bots.bot_controller import BotController
from bots.models import Bot, BotEventManager, BotEventSubTypes, BotEventTypes

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)


@shared_task(bind=True, soft_time_limit=3600)
def run_bot(self, bot_id):
    logger.info(f"Running bot {bot_id}")
    bot_controller = None
    try:
        bot_controller = BotController(bot_id)
        bot_controller.run()
    except SoftTimeLimitExceeded:
        logger.warning(f"Soft time limit exceeded for bot {bot_id}; recording timeout and cleaning up")
        try:
            bot = Bot.objects.get(id=bot_id)
            # Record a clear fatal error for visibility and state consistency
            BotEventManager.create_event(
                bot=bot,
                event_type=BotEventTypes.FATAL_ERROR,
                event_sub_type=BotEventSubTypes.FATAL_ERROR_HEARTBEAT_TIMEOUT,
            )
        except Exception as e:
            logger.error(f"Failed to record fatal heartbeat timeout for bot {bot_id}: {e}")
        finally:
            try:
                if bot_controller:
                    bot_controller.cleanup()
            except Exception as e:
                logger.error(f"Error during cleanup after soft time limit for bot {bot_id}: {e}")
        # Re-raise so Celery marks the task properly
        raise


def kill_child_processes():
    # Get the process group ID (PGID) of the current process
    pgid = os.getpgid(os.getpid())

    try:
        # Send SIGTERM to all processes in the process group
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass  # Process group may no longer exist


@worker_shutting_down.connect
def shutting_down_handler(sig, how, exitcode, **kwargs):
    # Just adding this code so we can see how to shut down all the tasks
    # when the main process is terminated.
    # It's likely overkill.
    logger.info("Celery worker shutting down, sending SIGTERM to all child processes")
    kill_child_processes()
