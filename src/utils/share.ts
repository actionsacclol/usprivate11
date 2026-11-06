// Centralised social-share helpers. We always credit @YuhgoSlavia and
// keep the wording short enough to fit in an X post comfortably.

export const X_PROFILE = '@YuhgoSlavia';
export const X_PROFILE_URL = 'https://x.com/YuhgoSlavia';

/** Open the user's browser to a pre-composed X (twitter) post. */
export async function shareToX(text: string): Promise<void> {
  const url = `https://x.com/intent/post?text=${encodeURIComponent(text)}`;
  try {
    await window.krypt.app.openExternal(url);
  } catch {
    // electron API not available — try window.open as a last resort.
    try { window.open(url, '_blank'); } catch { /* noop */ }
  }
}

/** Open the user's browser to follow @YuhgoSlavia. */
export async function followYuhgo(): Promise<void> {
  try {
    await window.krypt.app.openExternal(X_PROFILE_URL);
  } catch {
    try { window.open(X_PROFILE_URL, '_blank'); } catch { /* noop */ }
  }
}
