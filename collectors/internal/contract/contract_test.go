package contract_test

import (
	"encoding/json"
	"errors"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/Matt-LiFi/alph-e/collectors/internal/contract"
)

func sampleInput() contract.CollectorInput {
	start := time.Date(2026, time.April, 21, 14, 0, 0, 0, time.UTC)
	return contract.CollectorInput{
		IncidentID:    "inc_2a91",
		Question:      "Is db-primary showing connection errors in the last 15m?",
		HypothesisID:  "hyp_3",
		TimeRange:     contract.TimeRange{Start: start, End: start.Add(15 * time.Minute)},
		ScopeServices: []string{"db-primary"},
		EnvironmentFingerprint: contract.EnvironmentFingerprint{
			Cluster:           "prod-eu-west-1",
			Account:           "123456789012",
			Region:            "eu-west-1",
			DeployRevision:    "api@v2.14.3",
			RolloutGeneration: "api-7f9a",
		},
		MaxInternalIterations: 5,
	}
}

func TestCollectorInput_Validate(t *testing.T) {
	t.Parallel()

	t.Run("ok", func(t *testing.T) {
		require.NoError(t, sampleInput().Validate())
	})

	t.Run("missing incident_id", func(t *testing.T) {
		in := sampleInput()
		in.IncidentID = ""
		err := in.Validate()
		require.Error(t, err)
		assert.True(t, errors.Is(err, contract.ErrInvalidInput))
	})

	t.Run("end before start", func(t *testing.T) {
		in := sampleInput()
		in.TimeRange.End = in.TimeRange.Start.Add(-time.Minute)
		require.ErrorIs(t, in.Validate(), contract.ErrInvalidInput)
	})
}

func TestCollectorInput_EffectiveIterations(t *testing.T) {
	t.Parallel()
	in := sampleInput()
	in.MaxInternalIterations = 0
	assert.Equal(t, 5, in.EffectiveIterations())
	in.MaxInternalIterations = 99
	assert.Equal(t, 10, in.EffectiveIterations())
	in.MaxInternalIterations = 3
	assert.Equal(t, 3, in.EffectiveIterations())
}

func TestCollectorInput_JSONRoundtrip(t *testing.T) {
	t.Parallel()
	orig := sampleInput()
	data, err := json.Marshal(orig)
	require.NoError(t, err)

	var restored contract.CollectorInput
	require.NoError(t, json.Unmarshal(data, &restored))
	assert.Equal(t, orig, restored)
}
