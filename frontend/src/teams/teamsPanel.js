/**
 * Phase 13 (Teams meeting side panel): the small amount of Teams-specific
 * glue the listener page needs to run inside Teams as well as in a plain
 * browser tab.
 *
 * The listener page is deliberately unchanged in every other respect --
 * same WebSocket contract, same audio player, same state machine. Inside
 * Teams it is just this same page in an iframe, so the only real work is:
 *
 *   1. Initialising TeamsJS and calling app.notifySuccess(). This is NOT
 *      optional: Teams waits for that signal and shows "There was a
 *      problem reaching this app" if it never arrives, even though the
 *      page has loaded perfectly well. It is the single most common
 *      cause of a blank/erroring tab.
 *   2. Reporting whether we are in the 320px side panel, so the UI can
 *      lay itself out for a narrow column instead of a full page.
 *
 * Everything here fails soft. In a normal browser tab there is no Teams
 * host to talk to, initialize() rejects, and we report inTeams: false --
 * which must keep working, because the plain tab is how this page gets
 * developed and debugged.
 */

// Cached so multiple callers/StrictMode double-invocation don't each try
// to initialise the SDK.
let probePromise = null;

export const TEAMS_PANEL_STATE = {
  inTeams: false,
  frameContext: null,
  locale: null,
  displayName: null,
};

async function probe() {
  // Imported dynamically so a plain browser tab never pays for the SDK,
  // and so a missing/broken dependency degrades to "not in Teams"
  // instead of blanking the page.
  const teams = await import("@microsoft/teams-js");

  await teams.app.initialize();

  const context = await teams.app.getContext();

  // Tell Teams the app loaded. Without this the panel shows an error.
  teams.app.notifySuccess();

  return {
    inTeams: true,
    // "sidePanel" when rendered in the in-meeting side panel; "content"
    // in the meeting chat/details tabs; "meetingStage" when shared to
    // stage. See TeamsJS FrameContexts.
    frameContext: context?.page?.frameContext ?? null,
    // e.g. "en-us", "ru-ru" -- the user's Teams client locale. Not used
    // to pick a language in v1 (the picker stays explicit, see
    // MeetingListener.jsx), but read here because it is the obvious
    // basis for auto-selecting one later.
    locale: context?.app?.locale ?? null,
    // Phase 16: who is viewing this panel. Used to suppress the
    // translation of their own speech -- the bot attributes each phrase
    // to a Teams roster name, and this is the same name for the viewer.
    displayName: context?.user?.displayName ?? null,
  };
}

/**
 * Resolves to { inTeams, frameContext, locale }. Never rejects.
 */
export function initTeamsPanel() {
  if (!probePromise) {
    probePromise = probe()
      .then((state) => {
        Object.assign(TEAMS_PANEL_STATE, state);
        return state;
      })
      .catch(() => {
        // Not running inside Teams (a normal browser tab), or the host
        // never answered. Either way this page works standalone.
        return { inTeams: false, frameContext: null, locale: null, displayName: null };
      });
  }
  return probePromise;
}

/** True when rendered in the narrow in-meeting side panel specifically. */
export function isSidePanel(state) {
  return Boolean(state?.inTeams) && state.frameContext === "sidePanel";
}
