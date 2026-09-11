/**
 * Phase 13: the configuration page Teams opens when someone adds this app
 * to a meeting.
 *
 * Apps in meetings are declared as `configurableTabs` in the manifest,
 * and a configurable tab must point at a configurationUrl. Teams loads
 * that page in a dialog with Save disabled, and waits for the page to
 * (a) declare itself valid and (b) hand back the contentUrl to actually
 * render. Until both happen, Save stays greyed out -- which is what a
 * blank or broken config page looks like from the user's side.
 *
 * There is genuinely nothing to configure in v1: the meeting_id is fixed
 * in the contentUrl below and must match the bot's MEETING_ID. So this
 * page declares itself valid immediately and saves a fixed contentUrl.
 *
 * Why meeting_id isn't taken from the Teams context: getContext() does
 * expose the real Teams meeting/chat ID, but the bot names its meeting
 * independently via MEETING_ID in bot/.env, and the two have no reason to
 * agree. Wiring that up means teaching the bot to read the meeting ID off
 * its own Chromium page and use it as the room name -- a sensible
 * follow-up, but it would be the only untested moving part in the first
 * version of this panel, so v1 keeps the explicit, boring value.
 */

const MEETING_ID = "dev-meeting";

async function main() {
  const statusEl = document.getElementById("status");

  try {
    const teams = await import("@microsoft/teams-js");
    await teams.app.initialize();

    const contentUrl = `${window.location.origin}/listener.html?meeting_id=${encodeURIComponent(MEETING_ID)}`;

    teams.pages.config.registerOnSaveHandler((saveEvent) => {
      teams.pages.config
        .setConfig({
          entityId: `translation-panel-${MEETING_ID}`,
          contentUrl,
          websiteUrl: contentUrl,
          suggestedDisplayName: "Live Translation",
        })
        .then(() => saveEvent.notifySuccess())
        .catch((err) => saveEvent.notifyFailure(String(err)));
    });

    // Enables the Save button. Nothing to validate, so do it at once.
    teams.pages.config.setValidityState(true);

    statusEl.textContent = `Ready. This panel will show live translation for meeting "${MEETING_ID}". Select Save to add it.`;
  } catch (err) {
    statusEl.textContent =
      "This page has to be opened from inside Microsoft Teams. " +
      `(${err && err.message ? err.message : err})`;
  }
}

main();
