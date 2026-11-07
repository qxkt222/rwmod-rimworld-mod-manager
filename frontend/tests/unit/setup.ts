/**
 * Vitest setup — MSW server lifecycle.
 *
 * No handlers are registered yet: this is the skeleton the React wave fills
 * in. Keeping the lifecycle here proves `msw/node` boots inside happy-dom and
 * makes a request-mocking regression visible on day one.
 */
import { afterAll, afterEach, beforeAll } from "vitest";
import { setupServer } from "msw/node";

export const server = setupServer();

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());
