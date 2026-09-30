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
import java.io.File;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.UUID;

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
 */
public final class WarmRouter {
  public static void main(String[] args) throws Exception {
    PrintStream reply = System.out;
    System.setOut(System.err);
    GlobalSettings.setUserDataPath(Path.of(args[0]));
    GlobalSettings.lockUserDataPath();
    FRLogger.disableLogging();
    FRAnalytics.setEnabled(false);
    BufferedReader in = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
    reply.println("VELATRACE_WARM_READY");
    reply.flush();
    for (String line; (line = in.readLine()) != null; ) {
      String state;
      try {
        state = route(line.split("\t"));
      } catch (Throwable error) {
        error.printStackTrace();
        state = "ERROR";
      }
      reply.println("VELATRACE_JOB " + state);
      reply.flush();
    }
    System.exit(0);
  }

  private static String route(String[] args) throws Exception {
    GlobalSettings settings = new GlobalSettings();
    settings.applyEnvironmentVariables();
    settings.applyCommandLineArguments(args);
    Freerouting.globalSettings = settings;
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
    thread.start();
    thread.join();
    System.err.println(GsonProvider.GSON.toJson(new BoardStatistics(job.board)));
    if (job.state == RoutingJobState.COMPLETED) {
      Files.write(output.toPath(), job.output.getData().readAllBytes());
    }
    return job.state.name();
  }
}
