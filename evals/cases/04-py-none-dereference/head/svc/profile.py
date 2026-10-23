def find_user(store, user_id):
    return store.get(user_id)


def display_name(store, user_id):
    user = find_user(store, user_id)
    return user["first"] + " " + user["last"]
