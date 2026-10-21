import Dashboard from "../components/Dashboard";
import { AuthProvider } from "../lib/auth-context";
export default function Home() { return <AuthProvider><Dashboard /></AuthProvider>; }
