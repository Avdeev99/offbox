# Compose port parser fixtures

These synthetic fixtures use the Compose `ps --format json` service/Publishers shape. The array and NDJSON variants cover the supported output envelopes; service names and ports are test data, not captured runtime results. Only published TCP ports are forwarded; target ports, UDP, unpublished zero and IPv4/IPv6 duplicate bindings are deliberately present to detect incorrect discovery.
