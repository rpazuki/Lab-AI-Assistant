import { backendBaseUrl, sessionCookie } from "@/lib/config";

/**
 * The API did not answer at all, said in the shape the client reads.
 *
 * Letting the `fetch` below throw would end the route handler, and Next turns
 * that into an HTML 500 the typed client cannot parse into problem details: the
 * browser would show a bare status line, or nothing a person can act on. The two
 * ways this happens in practice are a deploy restarting the API container and a
 * request that outlives the gateway, so each gets its own words.
 */
function unreachable(caught: unknown): Response {
  const code = String((caught as { cause?: { code?: string } })?.cause?.code ?? "");
  const timedOut = code.includes("TIMEOUT");
  const problem = timedOut
    ? {
        status: 504,
        detail:
          "The API took too long to answer. The work may still be running: wait a " +
          "moment and reload the page before pressing again.",
      }
    : {
        status: 502,
        detail:
          "The API could not be reached: it may be restarting after an update. " +
          "Try again in a minute.",
      };
  return Response.json(
    {
      type: "https://lab-ai-assistant.invalid/problems/source_unavailable",
      title: "source unavailable",
      error_class: "source_unavailable",
      ...problem,
    },
    { status: problem.status },
  );
}

/**
 * Same-origin proxy to the API.
 *
 * Attaches the session cookie as a bearer token server-side and streams the
 * response through untouched, which matters for server-sent events: buffering the
 * body here would defeat token streaming.
 */
async function proxy(request: Request, path: string[]): Promise<Response> {
  const token = request.headers
    .get("cookie")
    ?.split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(`${sessionCookie}=`))
    ?.slice(sessionCookie.length + 1);

  const url = new URL(request.url);
  const target = `${backendBaseUrl}/api/v1/${path.join("/")}${url.search}`;

  const headers = new Headers();
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  const workspace = request.headers.get("x-workspace");
  if (workspace) headers.set("x-workspace", workspace);
  const requestId = request.headers.get("x-request-id");
  if (requestId) headers.set("x-request-id", requestId);
  if (token) headers.set("authorization", `Bearer ${token}`);

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: request.method,
      headers,
      body: request.method === "GET" || request.method === "HEAD" ? undefined : request.body,
      // Required by undici when streaming a request body through.
      duplex: "half",
      cache: "no-store",
    } as RequestInit);
  } catch (caught) {
    return unreachable(caught);
  }

  const responseHeaders = new Headers(upstream.headers);
  responseHeaders.delete("content-encoding");
  responseHeaders.delete("content-length");
  return new Response(upstream.body, {
    status: upstream.status,
    headers: responseHeaders,
  });
}

export async function GET(request: Request, context: { params: Promise<{ path: string[] }> }) {
  return proxy(request, (await context.params).path);
}
export async function POST(request: Request, context: { params: Promise<{ path: string[] }> }) {
  return proxy(request, (await context.params).path);
}
export async function PUT(request: Request, context: { params: Promise<{ path: string[] }> }) {
  return proxy(request, (await context.params).path);
}
export async function PATCH(request: Request, context: { params: Promise<{ path: string[] }> }) {
  return proxy(request, (await context.params).path);
}
export async function DELETE(request: Request, context: { params: Promise<{ path: string[] }> }) {
  return proxy(request, (await context.params).path);
}
