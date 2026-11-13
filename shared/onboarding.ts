/**
 * Which onboarding a user has been through. Bump it when onboarding gains
 * something an EXISTING user should see: anyone whose saved `onboardingSeen`
 * is lower gets it once more on their next launch, as a replay (closable,
 * disclaimer read-only, nothing written unless they act in a step).
 *
 *  1 — before 3.0 (the field didn't exist; a missing value reads as 0)
 *  2 — 3.0: AI agents front and centre, Paper | Live, Autopilot
 *  3 — the bundled setup video on the welcome step
 *  4 — 0.4.0: the owner's guide video on YouTube, first on the welcome step
 *
 * Before this, onboarding opened only while the disclaimer was unaccepted, so
 * the whole agent setup would never reach anyone upgrading.
 */
export const ONBOARDING_REVISION = 4;

/** What should open by itself on launch, if anything. */
export function autoOnboarding(
  state: { acceptedDisclaimer: boolean; onboardingSeen?: number } | null | undefined,
): 'first' | 'update' | null {
  if (!state) return null;
  if (!state.acceptedDisclaimer) return 'first';
  return (state.onboardingSeen ?? 0) < ONBOARDING_REVISION ? 'update' : null;
}
