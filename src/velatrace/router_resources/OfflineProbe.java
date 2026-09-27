import java.io.FilePermission;
import java.net.SocketPermission;
import java.net.URLPermission;
import java.security.Permission;
import java.security.SecurityPermission;

/** Independent MIT-licensed policy test. Never opens a connection. */
@SuppressWarnings("removal")
public final class OfflineProbe {
  private static void denied(Permission p) {
    try { System.getSecurityManager().checkPermission(p); }
    catch (SecurityException expected) { return; }
    throw new IllegalStateException("Permission unexpectedly granted: " + p);
  }
  public static void main(String[] args) {
    if (System.getSecurityManager() == null) throw new IllegalStateException("No security manager");
    denied(new SocketPermission("*", "connect,accept,listen,resolve"));
    denied(new SocketPermission("api.github.com", "resolve"));
    denied(new SocketPermission("127.0.0.1:443", "connect"));
    denied(new URLPermission("https://api.github.com/*", "GET"));
    denied(new RuntimePermission("setSecurityManager"));
    denied(new SecurityPermission("setPolicy"));
    denied(new FilePermission("<<ALL FILES>>", "execute"));
    denied(new FilePermission(args[0], "write"));
    System.out.println("VELATRACE_OFFLINE_POLICY_OK");
  }
}
