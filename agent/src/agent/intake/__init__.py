"""Intake: surfaces the outside world sends incidents through.

MVP1 supports Alertmanager webhooks only. Slack / Linear / on-call paging are
stubbed here so their call sites have a stable signature the day someone wires
them up.
"""
