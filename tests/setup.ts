import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

class ResizeObserverStub {
  constructor(private readonly callback: ResizeObserverCallback) {}

  observe(target: Element) {
    this.callback(
      [
        {
          target,
          contentRect: {
            width: 900,
            height: 430,
            top: 0,
            right: 900,
            bottom: 430,
            left: 0,
            x: 0,
            y: 0,
            toJSON: () => ({}),
          },
        } as ResizeObserverEntry,
      ],
      this as unknown as ResizeObserver,
    );
  }
  unobserve() {}
  disconnect() {}
}

globalThis.ResizeObserver = ResizeObserverStub;

Object.defineProperties(HTMLElement.prototype, {
  clientWidth: { configurable: true, get: () => 900 },
  clientHeight: { configurable: true, get: () => 430 },
});

afterEach(() => cleanup());
