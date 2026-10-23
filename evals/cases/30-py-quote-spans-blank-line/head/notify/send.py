def send(client, msg):
    try:
        return client.post(msg)

    except Exception:
        return None
