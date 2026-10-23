import { db } from "./db.js";

export class UserRepo {
  async find(id) {
    return db.get("users", id);
  }
}
