import { NextResponse } from "next/server";
import { requestBoundaryError } from "./lib/request-boundary.mjs";

export function proxy(request) {
  const code = requestBoundaryError(request, process.env.DEVFLOW_PUBLIC_ORIGIN || "http://127.0.0.1:3000");
  if (code) {
    return NextResponse.json({ error: { code, message: "Request denied by the frontend local-origin boundary." } }, {
      status: 403, headers: { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" },
    });
  }
  return NextResponse.next();
}
