import app.freerouting.Freerouting;
import app.freerouting.board.ItemIdentificationNumberGenerator;
import app.freerouting.core.RoutingJob;
import app.freerouting.core.RoutingJobState;
import app.freerouting.core.scoring.BoardStatistics;
import app.freerouting.interactive.HeadlessBoardManager;
import app.freerouting.logger.FRLogger;
import app.freerouting.management.RoutingJobSchedulerActionThread;
import app.freerouting.management.analytics.FRAnalytics;
import app.freerouting.management.gson.GsonProvider;
import app.freerouting.settings.GlobalSettings;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.UUID;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * VelaTrace warm launcher (MIT). Runs jobs of the unmodified, hash-pinned
 * Freerouting 2.1.0 JAR in one JVM under the same offline policy as the CLI.
 *
 * Each stdin line is one CLI argument list (tab-separated). A job is the 2.1.0
 * CLI path (Freerouting.main + InitializeCLI + RoutingJobScheduler dispatch)
 * minus its 1 s start-up sleep, update check, analytics and 250/500 ms polling.
 * Settings are rebuilt from the arguments for every job. Replies one
 * "VELATRACE_JOB <state>" line per job on stdout; everything else goes to
 * stderr. Exits when stdin closes, so a dead parent never leaves Java running.
 *
 * A "VELATRACE_STOP" line during a job asks Freerouting to stop (its own
 * requestStop, as its job time-out does); the best finished pass is written as
 * the SES and the reply is "VELATRACE_JOB STOPPED". Freerouting 2.1.0 has no
 * working command-line pass or time limit, so this is the only way to keep a
 * partial result.
 */
public final class WarmRouter {
  private static final String STOP = "VELATRACE_STOP";
  private static final Pattern PASS = Pattern.compile("Auto-router pass #\\d+ .*?(?:\\((\\d+) unrouted[^)]*\\))?\\.?$");
  private static volatile RoutingJob current;
  private static volatile int bestUnrouted;
  private static volatile byte[] bestSes;

  public static void main(String[] args) throws Exception {
    PrintStream reply = System.out;
    System.setOut(System.err);
    GlobalSettings.setUserDataPath(Path.of(args[0]));
    GlobalSettings.lockUserDataPath();
    FRLogger.disableLogging();
    // File logging is off; echo Freerouting's messages (one per router pass) so the
    // parent can show progress and see when the router stops improving.
    FRLogger.getLogEntries().addLogEntryAddedListener(entry -> {
      System.err.println(entry.getMessage());
      keepBest(entry.getMessage());
    });
    FRAnalytics.setEnabled(false);
    BufferedReader in = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
    reply.println("VELATRACE_WARM_READY");
    reply.flush();
    for (String line; (line = in.readLine()) != null; ) {
      if (line.equals(STOP)) continue;  // Arrived after its job had finished.
      String state;
      try {
        state = route(line.split("\t"), in);
      } catch (Throwable error) {
        error.printStackTrace();
        state = "ERROR";
      }
      reply.println("VELATRACE_JOB " + state);
      reply.flush();
    }
    System.exit(0);
  }

  /**
   * Freerouting logs each finished pass from its router thread, when the board is
   * consistent. Keep the SES of the pass with the fewest unrouted connections (the
   * latest among equals): a stop request usually lands in the middle of a later
   * pass, whose ripped-up board is worse than the one that pass started from.
   */
  private static void keepBest(String message) {
    RoutingJob job = current;
    if (job == null || Thread.currentThread() != job.thread || message == null) return;
    Matcher pass = PASS.matcher(message);
    if (!pass.find()) return;
    int unrouted = pass.group(1) == null ? 0 : Integer.parseInt(pass.group(1));
    if (unrouted > bestUnrouted) return;
    ByteArrayOutputStream ses = new ByteArrayOutputStream();
    HeadlessBoardManager manager = new HeadlessBoardManager(null, job);
    manager.replaceRoutingBoard(job.board);
    if (manager.saveAsSpecctraSessionSes(ses, job.name)) {
      bestUnrouted = unrouted;
      bestSes = ses.toByteArray();
    }
  }

  private static String route(String[] args, BufferedReader in) throws Exception {
    GlobalSettings settings = new GlobalSettings();
    settings.applyEnvironmentVariables();
    settings.applyCommandLineArguments(args);
    Freerouting.globalSettings = settings;
    FRLogger.getLogEntries().clear();  // Kept in memory by Freerouting; one job's worth is enough.
    RoutingJob job = new RoutingJob(UUID.randomUUID());
    job.setInput(settings.design_input_filename);
    File output = new File(settings.design_output_filename);
    output.delete();
    job.tryToSetOutputFile(output);
    job.routerSettings = settings.routerSettings.clone();
    job.routerSettings.setLayerCount(job.input.statistics.layers.totalCount);
    HeadlessBoardManager manager = new HeadlessBoardManager(null, job);
    manager.loadFromSpecctraDsn(job.input.getData(), null, new ItemIdentificationNumberGenerator());
    job.board = manager.get_routing_board();
    RoutingJobSchedulerActionThread thread = new RoutingJobSchedulerActionThread(job);
    job.thread = thread;
    job.state = RoutingJobState.RUNNING;
    bestUnrouted = Integer.MAX_VALUE;
    bestSes = null;
    current = job;
    thread.start();
    boolean stopped = false;
    while (thread.isAlive()) {
      thread.join(100);
      if (!stopped && in.ready()) {
        String command = in.readLine();
        if (command == null) System.exit(0);  // Parent gone.
        if (command.equals(STOP)) {
          stopped = true;
          thread.requestStop();
        }
      }
    }
    System.err.println(GsonProvider.GSON.toJson(new BoardStatistics(job.board)));
    current = null;
    if (stopped) {
      if (bestSes == null) return "ERROR";  // Stopped before any pass finished: nothing worth keeping.
      Files.write(output.toPath(), bestSes);
      return "STOPPED";
    }
    if (job.state == RoutingJobState.COMPLETED) {
      Files.write(output.toPath(), job.output.getData().readAllBytes());
    }
    return job.state.name();
  }
}
