# Fix Encryption Overhead in Spec

## Issue

The spec (Appendix A) claims 40 bytes encryption overhead, but actual implementation has:
- **56 bytes** for symmetric encryption (16 id + 24 nonce + 16 Poly1305 tag)
- **88 bytes** for asymmetric sealing (16 id + 32 ephemeral pubkey + 24 nonce + 16 tag)

## Location

`docs/quiet-protocol-specification.md` - search for "40 bytes" in the Event-Layer Encryption section.

## Fix

Update the spec to reflect accurate overhead numbers.
