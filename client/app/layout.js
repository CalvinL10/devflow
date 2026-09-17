import "./globals.css";

export const metadata = {
  title: "DevFlow Workbench",
  description: "Backend-authoritative coding workflow console",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
