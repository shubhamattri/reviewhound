package store

import "database/sql"

func ByID(db *sql.DB, id int) (*sql.Rows, error) {
	return db.Query("SELECT * FROM users WHERE id = $1", id)
}
