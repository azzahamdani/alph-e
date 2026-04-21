// Package server wraps a Collector implementation in a tiny HTTP surface.
//
// Every binary under cmd/ registers exactly one Collector and serves it on
// POST /collect. Health is exposed on GET /healthz. Nothing else.
package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"time"

	"github.com/Matt-LiFi/alph-e/collectors/internal/contract"
)

// Collector is the behaviour each binary implements.
type Collector interface {
	Name() string
	Collect(ctx context.Context, in contract.CollectorInput) (contract.CollectorOutput, error)
}

// Server holds a named Collector and exposes the /collect + /healthz handlers.
type Server struct {
	Collector Collector
	Logger    *slog.Logger
}

// Handler returns the HTTP handler. Defaults a logger if none is set.
func (s *Server) Handler() http.Handler {
	if s.Logger == nil {
		s.Logger = slog.Default()
	}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /collect", s.handleCollect)
	mux.HandleFunc("GET /healthz", s.handleHealth)
	return mux
}

type errorBody struct {
	Error string `json:"error"`
}

func (s *Server) handleCollect(w http.ResponseWriter, r *http.Request) {
	var in contract.CollectorInput
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(&in); err != nil {
		s.writeJSON(w, http.StatusBadRequest, errorBody{Error: "invalid JSON: " + err.Error()})
		return
	}
	if err := in.Validate(); err != nil {
		s.writeJSON(w, http.StatusBadRequest, errorBody{Error: err.Error()})
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()

	out, err := s.Collector.Collect(ctx, in)
	if err != nil {
		s.Logger.Error("collect failed",
			slog.String("collector", s.Collector.Name()),
			slog.String("incident_id", in.IncidentID),
			slog.String("hypothesis_id", in.HypothesisID),
			slog.String("err", err.Error()),
		)
		status := http.StatusInternalServerError
		if errors.Is(err, contract.ErrInvalidInput) {
			status = http.StatusBadRequest
		}
		s.writeJSON(w, status, errorBody{Error: err.Error()})
		return
	}
	s.writeJSON(w, http.StatusOK, out)
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	s.writeJSON(w, http.StatusOK, map[string]string{
		"status":    "ok",
		"collector": s.Collector.Name(),
	})
}

func (s *Server) writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(body); err != nil {
		s.Logger.Error("json encode failed", slog.String("err", err.Error()))
	}
}

// ListenAndServe is the common main-loop helper.
func (s *Server) ListenAndServe(addr string) error {
	srv := &http.Server{
		Addr:              addr,
		Handler:           s.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	s.Logger.Info("collector serving",
		slog.String("collector", s.Collector.Name()),
		slog.String("addr", addr),
	)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		return fmt.Errorf("listen: %w", err)
	}
	return nil
}
