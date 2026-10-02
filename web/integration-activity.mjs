// Keep the trial workspace stable while an asynchronous parse or save is pending.
export function createActivityLock(workspace) {
  let busy = false;
  return {
    get busy() { return busy; },
    async run(action) {
      if (busy) return false;
      busy = true;
      workspace.inert = true;
      workspace.setAttribute("aria-busy", "true");
      try {
        await action();
        return true;
      } finally {
        busy = false;
        workspace.inert = false;
        workspace.removeAttribute("aria-busy");
      }
    },
  };
}
