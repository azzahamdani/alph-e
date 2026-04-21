// Package contract mirrors the Pydantic v2 models in
// agent/src/agent/schemas/collector.py. Keep field names and JSON tags
// byte-for-byte identical — the two sides speak JSON over HTTP and nothing
// else, so drift is silent.
package contract

import "time"

// TimeRange is the half-open [start, end) interval a collector query runs over.
type TimeRange struct {
	Start time.Time `json:"start"`
	End   time.Time `json:"end"`
}

// EnvironmentFingerprint identifies the cluster/account/region/revision
// the incident is about. Included in the collector cache key.
type EnvironmentFingerprint struct {
	Cluster           string `json:"cluster"`
	Account           string `json:"account"`
	Region            string `json:"region"`
	DeployRevision    string `json:"deploy_revision"`
	RolloutGeneration string `json:"rollout_generation"`
}

// EvidenceRef is the metadata envelope for a blob in the evidence store.
type EvidenceRef struct {
	EvidenceID  string    `json:"evidence_id"`
	StorageURI  string    `json:"storage_uri"`
	ContentType string    `json:"content_type"`
	SizeBytes   int64     `json:"size_bytes"`
	ExpiresAt   time.Time `json:"expires_at"`
}

// Finding is the one-question-one-answer refined collector output.
type Finding struct {
	ID                 string    `json:"id"`
	CollectorName      string    `json:"collector_name"`
	Question           string    `json:"question"`
	Summary            string    `json:"summary"`
	EvidenceID         string    `json:"evidence_id"`
	Confidence         float64   `json:"confidence"`
	SuggestedFollowups []string  `json:"suggested_followups"`
	CreatedAt          time.Time `json:"created_at"`
}

// CollectorInput is what the orchestrator POSTs to a collector.
type CollectorInput struct {
	IncidentID             string                 `json:"incident_id"`
	Question               string                 `json:"question"`
	HypothesisID           string                 `json:"hypothesis_id"`
	TimeRange              TimeRange              `json:"time_range"`
	ScopeServices          []string               `json:"scope_services"`
	EnvironmentFingerprint EnvironmentFingerprint `json:"environment_fingerprint"`
	// MaxInternalIterations is the cap on the collector's own tool-use loop.
	// Defaults to 5 if zero or negative.
	MaxInternalIterations int `json:"max_internal_iterations"`
}

// CollectorOutput is what the orchestrator receives back.
type CollectorOutput struct {
	Finding       Finding     `json:"finding"`
	Evidence      EvidenceRef `json:"evidence"`
	ToolCallsUsed int         `json:"tool_calls_used"`
	TokensUsed    int         `json:"tokens_used"`
}

// Validate returns a descriptive error if the CollectorInput is malformed.
// The orchestrator pre-validates too, but defence-in-depth is cheap.
func (c CollectorInput) Validate() error {
	switch {
	case c.IncidentID == "":
		return errField("incident_id is required")
	case c.Question == "":
		return errField("question is required")
	case c.HypothesisID == "":
		return errField("hypothesis_id is required")
	case c.TimeRange.Start.IsZero() || c.TimeRange.End.IsZero():
		return errField("time_range.start and time_range.end are required")
	case !c.TimeRange.End.After(c.TimeRange.Start):
		return errField("time_range.end must be strictly after time_range.start")
	case c.EnvironmentFingerprint.Cluster == "":
		return errField("environment_fingerprint.cluster is required")
	default:
		return nil
	}
}

// EffectiveIterations clamps MaxInternalIterations into [1, 10]; defaulting to 5.
func (c CollectorInput) EffectiveIterations() int {
	switch {
	case c.MaxInternalIterations <= 0:
		return 5
	case c.MaxInternalIterations > 10:
		return 10
	default:
		return c.MaxInternalIterations
	}
}
