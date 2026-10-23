package api

import "net/http"

type Server struct {
	users UserStore
}

func (s *Server) requireAdmin(r *http.Request) error {
	if r.Header.Get("X-Role") != "admin" {
		return errForbidden
	}
	return nil
}

func (s *Server) GetUser(w http.ResponseWriter, r *http.Request) {
	if err := s.requireAdmin(r); err != nil {
		http.Error(w, "forbidden", http.StatusForbidden)
		return
	}
	writeJSON(w, s.users.Get(r.URL.Query().Get("id")))
}
