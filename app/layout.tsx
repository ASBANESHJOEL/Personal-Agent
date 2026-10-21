import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = { title: "Personal Agent · Control room", description: "A safe control room for your developer presence." };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) { return <html lang="en"><body>{children}</body></html>; }
