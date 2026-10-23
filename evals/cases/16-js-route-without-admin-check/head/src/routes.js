import { requireAdmin } from "./auth.js";
import { getUser, updateUser, deleteUser } from "./users.js";

export function register(app) {
  app.get("/users/:id", requireAdmin, getUser);
  app.put("/users/:id", requireAdmin, updateUser);
  app.delete("/users/:id", deleteUser);
}
