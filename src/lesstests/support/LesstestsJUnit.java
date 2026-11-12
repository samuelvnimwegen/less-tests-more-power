package lesstests;

import java.io.IOException;
import java.lang.reflect.AnnotatedElement;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.junit.jupiter.api.Disabled;
import org.junit.platform.commons.annotation.Testable;
import org.junit.platform.commons.support.AnnotationSupport;
import org.junit.platform.commons.support.HierarchyTraversalMode;
import org.junit.platform.commons.support.ReflectionSupport;
import org.junit.platform.engine.DiscoverySelector;
import org.junit.platform.engine.TestExecutionResult;
import org.junit.platform.engine.TestSource;
import org.junit.platform.engine.discovery.ClassNameFilter;
import org.junit.platform.engine.discovery.DiscoverySelectors;
import org.junit.platform.engine.support.descriptor.ClassSource;
import org.junit.platform.engine.support.descriptor.MethodSource;
import org.junit.platform.launcher.Launcher;
import org.junit.platform.launcher.LauncherDiscoveryRequest;
import org.junit.platform.launcher.TestExecutionListener;
import org.junit.platform.launcher.TestIdentifier;
import org.junit.platform.launcher.TestPlan;
import org.junit.platform.launcher.core.LauncherDiscoveryRequestBuilder;
import org.junit.platform.launcher.core.LauncherFactory;

/**
 * Lists and runs JUnit 5 test methods for lesstests, which knows a test as a class name and a method name.
 *
 * lesstests compiles this file against the test classpath of its copy of the project, so the JUnit Platform of
 * the project itself does the work. "discover OUT ROOT..." writes every test method under the class folders, without
 * the disabled ones, in the order JUnit runs them, so @Order and the class order of the project hold. "run REPORTS
 * IDS" runs the methods listed in IDS and writes REPORTS/results.tsv with one outcome per method. A parameterized
 * method or a test factory gets its worst child. A method whose class, or an enclosing class, failed before it ran
 * counts as failed.
 */
public final class LesstestsJUnit {

    private static final List<String> WORST_FIRST = List.of("FAIL", "SKIP", "PASS");

    private LesstestsJUnit() {
    }

    public static void main(String[] args) throws Exception {
        if (args.length >= 3 && args[0].equals("discover")) {
            List<Path> roots = new ArrayList<>();
            for (int index = 2; index < args.length; index++) {
                roots.add(Path.of(args[index]));
            }
            discover(Path.of(args[1]), roots);
        } else if (args.length == 3 && args[0].equals("run")) {
            run(Path.of(args[1]), Files.readAllLines(Path.of(args[2]), StandardCharsets.UTF_8));
        } else {
            throw new IllegalArgumentException("Use: discover OUT ROOT... | run REPORTS IDS");
        }
    }

    private static void discover(Path out, List<Path> roots) throws IOException {
        // JUnit only picks classes named like Test* or *Tests by default. Gradle runs every class, so this does too.
        LauncherDiscoveryRequest request = LauncherDiscoveryRequestBuilder.request()
                .selectors(DiscoverySelectors.selectClasspathRoots(new LinkedHashSet<>(roots)))
                .filters(ClassNameFilter.includeClassNamePatterns(".*"))
                .build();
        TestPlan plan = LauncherFactory.create().discover(request);
        Set<String> lines = new LinkedHashSet<>();
        for (TestIdentifier root : plan.getRoots()) {
            for (TestIdentifier identifier : plan.getDescendants(root)) {
                TestSource source = identifier.getSource().orElse(null);
                if (source instanceof MethodSource method && !disabled(method)) {
                    lines.add(method.getClassName() + "\t" + method.getMethodName());
                }
            }
        }
        Files.write(out, lines, StandardCharsets.UTF_8);
    }

    private static boolean disabled(MethodSource source) {
        if (isDisabled(source.getJavaMethod())) {
            return true;
        }
        for (Class<?> type = source.getJavaClass(); type != null; type = type.getEnclosingClass()) {
            if (isDisabled(type)) {
                return true;
            }
        }
        return false;
    }

    private static boolean isDisabled(AnnotatedElement element) {
        return AnnotationSupport.isAnnotated(element, Disabled.class);
    }

    private static void run(Path reports, List<String> ids) throws Exception {
        Map<String, String> outcomes = new LinkedHashMap<>();
        List<DiscoverySelector> selectors = new ArrayList<>();
        ClassLoader loader = Thread.currentThread().getContextClassLoader();
        for (String id : ids) {
            if (id.isBlank()) {
                continue;
            }
            String[] parts = id.split("\t", 2);
            outcomes.put(id, null);
            Class<?> type;
            try {
                type = Class.forName(parts[0], false, loader);
            } catch (ClassNotFoundException gone) {
                // One class that is gone only loses its own tests. They stay MISSING.
                continue;
            }
            List<Method> methods = ReflectionSupport.findMethods(type,
                    method -> method.getName().equals(parts[1]) && AnnotationSupport.isAnnotated(method, Testable.class),
                    HierarchyTraversalMode.TOP_DOWN);
            for (Method method : methods) {
                selectors.add(DiscoverySelectors.selectMethod(type, method));
            }
        }
        Outcomes listener = new Outcomes(outcomes);
        Launcher launcher = LauncherFactory.create();
        launcher.execute(LauncherDiscoveryRequestBuilder.request().selectors(selectors).build(), listener);
        Files.createDirectories(reports);
        List<String> lines = new ArrayList<>();
        for (String id : outcomes.keySet()) {
            lines.add(id + "\t" + listener.outcome(id));
        }
        Files.write(reports.resolve("results.tsv"), lines, StandardCharsets.UTF_8);
    }

    /** Keeps the worst outcome per method, and which classes failed or got skipped as a whole. */
    private static final class Outcomes implements TestExecutionListener {

        private final Map<String, String> outcomes;
        private final Map<String, String> classes = new LinkedHashMap<>();
        private TestPlan plan;

        Outcomes(Map<String, String> outcomes) {
            this.outcomes = outcomes;
        }

        @Override
        public void testPlanExecutionStarted(TestPlan testPlan) {
            plan = testPlan;
        }

        @Override
        public void executionSkipped(TestIdentifier identifier, String reason) {
            keep(identifier, "SKIP");
        }

        @Override
        public void executionFinished(TestIdentifier identifier, TestExecutionResult result) {
            String status = switch (result.getStatus()) {
                case SUCCESSFUL -> "PASS";
                case ABORTED -> "SKIP";
                case FAILED -> "FAIL";
            };
            result.getThrowable().ifPresent(error -> {
                System.err.println("lesstests: " + identifier.getDisplayName() + " " + status);
                error.printStackTrace();
            });
            keep(identifier, status);
        }

        private void keep(TestIdentifier identifier, String status) {
            TestSource source = methodOf(identifier);
            if (source instanceof MethodSource method) {
                String id = method.getClassName() + "\t" + method.getMethodName();

                // A parameterized method also finishes once as a container, after its invocations.
                if (outcomes.containsKey(id)) {
                    outcomes.put(id, worse(outcomes.get(id), status));
                }
            } else if (source instanceof ClassSource type && !status.equals("PASS")) {
                classes.merge(type.getClassName(), status, LesstestsJUnit::worse);
            }
        }

        /** Finds the method a test belongs to. A dynamic test of a factory only has one through its parents. */
        private TestSource methodOf(TestIdentifier identifier) {
            for (TestIdentifier current = identifier; current != null; current = plan.getParent(current).orElse(null)) {
                TestSource source = current.getSource().orElse(null);
                if (source instanceof MethodSource || source instanceof ClassSource) {
                    return source;
                }
            }
            return null;
        }

        String outcome(String id) {
            String found = outcomes.get(id);
            if (found != null) {
                return found;
            }
            // A nested class fails with its outer class, so look at every enclosing class too.
            String type = id.split("\t", 2)[0];
            while (true) {
                String failed = classes.get(type);
                if (failed != null) {
                    return failed;
                }
                int inner = type.lastIndexOf('$');
                if (inner < 0) {
                    return "MISSING";
                }
                type = type.substring(0, inner);
            }
        }
    }

    private static String worse(String one, String other) {
        if (one == null) {
            return other;
        }
        return WORST_FIRST.indexOf(one) <= WORST_FIRST.indexOf(other) ? one : other;
    }
}
