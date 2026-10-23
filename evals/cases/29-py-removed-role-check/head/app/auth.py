def require_role(request, role):
    if role not in request.user.roles:
        raise PermissionError(role)
