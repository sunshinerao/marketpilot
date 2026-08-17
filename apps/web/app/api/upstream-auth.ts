import { NextRequest } from "next/server";

export function upstreamHeaders(
  request: NextRequest,
  options: { json?: boolean } = {},
): Headers {
  const headers = new Headers();
  const authorization = request.headers.get("authorization");
  if (authorization) headers.set("authorization", authorization);
  if (options.json) headers.set("content-type", "application/json");
  return headers;
}

export function upstreamResponseHeaders(status: number): Headers {
  const headers = new Headers({ "cache-control": "no-store" });
  if (status === 401) headers.set("www-authenticate", "Bearer");
  return headers;
}

export class UpstreamHttpError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}
