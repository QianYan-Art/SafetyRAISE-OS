import { useEffect, type RefObject } from "react";

const activeDialogs: HTMLElement[] = [];

// 嵌套确认框只约束最上层，关闭后恢复原先的背景状态与焦点。
export function useDialogFocus(ref: RefObject<HTMLElement>, open: boolean) {
  useEffect(() => {
    const dialog = ref.current;
    if (!open || !dialog) return;
    const previous = document.activeElement as HTMLElement | null;
    const isolated: Array<[HTMLElement, boolean]> = [];
    let branch: HTMLElement = dialog;
    while (branch.parentElement && branch.parentElement !== document.body) {
      for (const sibling of Array.from(branch.parentElement.children)) {
        if (sibling !== branch && sibling instanceof HTMLElement) {
          isolated.push([sibling, sibling.inert]);
          sibling.inert = true;
        }
      }
      branch = branch.parentElement;
    }
    activeDialogs.push(dialog);
    const controls = () => Array.from(dialog.querySelectorAll<HTMLElement>(
      'button:not([disabled]),input:not([disabled]),textarea:not([disabled]),select:not([disabled]),a[href],[tabindex="0"]',
    )).filter(node => !node.closest("[hidden]") && getComputedStyle(node).display !== "none" && getComputedStyle(node).visibility !== "hidden");
    const isTop = () => activeDialogs[activeDialogs.length - 1] === dialog;
    controls()[0]?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (!isTop() || event.key !== "Tab") return;
      const nodes = controls(), first = nodes[0], last = nodes[nodes.length - 1];
      if (!first) { event.preventDefault(); return; }
      if (event.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !dialog.contains(document.activeElement))) {
        event.preventDefault(); first.focus();
      }
    };
    const onFocus = (event: FocusEvent) => {
      if (isTop() && event.target instanceof Node && !dialog.contains(event.target)) controls()[0]?.focus();
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("focusin", onFocus);
    return () => {
      activeDialogs.splice(activeDialogs.indexOf(dialog), 1);
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("focusin", onFocus);
      isolated.forEach(([node, wasInert]) => { node.inert = wasInert; });
      if (previous?.isConnected && !previous.closest("[inert]")) previous.focus();
    };
  }, [open, ref]);
}
