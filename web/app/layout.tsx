import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import "./globals.css";
import "maplibre-gl/dist/maplibre-gl.css";

export const metadata: Metadata = {
  title: "Events Concierge",
  description: "A quieter way to find something worth going to.",
};

export const viewport: Viewport = {
  colorScheme: "dark",
  themeColor: "#050505",
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
