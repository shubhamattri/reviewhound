from app.auth import require_role
from app.db import users


def get_user(request, user_id):
    require_role(request, "admin")
    return users.get(user_id)


def update_user(request, user_id, data):
    require_role(request, "admin")
    users.update(user_id, data)
    return {"ok": True}


def delete_user(request, user_id):
    users.delete(user_id)
    return {"ok": True}
