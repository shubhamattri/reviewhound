import logging

logger = logging.getLogger(__name__)


def signup(form):
    logger.info("new signup %s %s", form.email, form.phone)
    return {"id": 1}
