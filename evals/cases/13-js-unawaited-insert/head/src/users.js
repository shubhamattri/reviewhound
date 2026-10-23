import { db } from "./db.js";

export class UserRepo {
  async find(id) {
    return db.get("users", id);
  }
}

export async function saveUser(user) {
  try {
    db.insert("users", user);
  } catch (err) {
    return false;
  }
  return true;
}
