/**
 * Shape checks for a pasted Polymarket US API key, so a bad paste fails on the
 * screen rather than as a confusing error from the exchange. Polymarket decides
 * whether the key is real; this only decides whether it could be one.
 */

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
// Standard or URL-safe base64. An Ed25519 key is 32 bytes (44 characters
// encoded); some exports append the 32-byte public half.
const BASE64 = /^[A-Za-z0-9+/_-]{40,}={0,2}$/;

/** Tidy a paste: stray spaces and quotes go, and two values in the wrong boxes swap.
 *  Swapping is safe because a UUID can never be a valid Secret Key. */
export function normalizePastedKey(keyId: string, secretKey: string): { keyId: string; secretKey: string } {
  const clean = (v: string): string => v.trim().replace(/^["']|["'],?$/g, '').trim();
  const id = clean(keyId);
  const secret = clean(secretKey);
  if (!UUID.test(id) && UUID.test(secret)) return { keyId: secret, secretKey: id };
  return { keyId: id, secretKey: secret };
}

/** Why this key can't be saved, or null when it looks right. */
export function apiKeyProblem(keyId: string, secretKey: string): string | null {
  if (!UUID.test(keyId.trim())) {
    return 'The Key ID should be a UUID, like 123e4567-e89b-12d3-a456-426614174000.';
  }
  if (!BASE64.test(secretKey.trim())) {
    return 'The Secret Key should be the long base64 string Polymarket US showed when you created the key.';
  }
  return null;
}
