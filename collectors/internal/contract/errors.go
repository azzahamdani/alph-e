package contract

import "errors"

// ErrInvalidInput is the sentinel returned when a CollectorInput fails validation.
// Downstream handlers match on this to map to HTTP 400.
var ErrInvalidInput = errors.New("invalid collector input")

func errField(msg string) error {
	return fieldError{msg: msg}
}

type fieldError struct{ msg string }

func (e fieldError) Error() string { return e.msg }
func (e fieldError) Unwrap() error { return ErrInvalidInput }
func (e fieldError) Is(target error) bool {
	return errors.Is(target, ErrInvalidInput)
}
