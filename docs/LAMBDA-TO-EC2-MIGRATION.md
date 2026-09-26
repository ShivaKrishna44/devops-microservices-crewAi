# Migrating a Java Lambda Function to a Single EC2 Instance

A step-by-step runbook for moving an existing, live AWS Lambda function
(Java runtime) onto a single EC2 instance. Covers every common Lambda
trigger type, since the replacement mechanism is different for each one —
read the section matching your Lambda's actual trigger(s) first, then come
back for the shared steps (packaging, IAM, deployment, cutover, rollback).

> **Before you start:** confirm exactly which trigger(s) are attached to the
> Lambda today. Open AWS Console → Lambda → your function → **Configuration
> → Triggers** tab, or run:
> ```bash
> aws lambda get-function --function-name <your-function-name> --query "Configuration.[FunctionName,Runtime,Handler,Timeout,MemorySize,Environment]"
> aws lambda list-event-source-mappings --function-name <your-function-name>
> ```
> `list-event-source-mappings` shows poll-based triggers (SQS, DynamoDB
> Streams, Kinesis). Push-based triggers (API Gateway, S3, SNS, EventBridge)
> won't show up there — check each service's own console/CLI instead (exact
> commands are in each section below).

---

## 0. Why migrate Lambda → EC2 (write this down before you start)

Be explicit about the actual driver, because it affects design decisions
below (timeout limits, cost model, connection handling):

- **Execution time limit** — Lambda hard-caps at 15 minutes; if the job
  needs longer, EC2 removes that ceiling entirely.
- **Cold start latency** — Java Lambda cold starts (JVM init) are the worst
  of any Lambda runtime, often 1-5+ seconds. An always-on EC2 process has
  zero cold start after the JVM warms up once at boot.
- **Persistent connections / local state** — Lambda's stateless,
  short-lived execution model fights against connection pooling, in-memory
  caching, or long-lived sockets. EC2 can hold a real, warm connection pool.
- **Cost at sustained high volume** — Lambda's per-invocation pricing can
  exceed a reserved/on-demand EC2 instance's flat cost past a certain
  request volume; do the math for your actual traffic before committing.
- **Something else** — write your real reason here so the design section
  below is easy to justify later.

---

## 1. Inventory the Lambda completely before touching anything

Pull every piece of config you'll need to replicate on EC2. Nothing here
changes anything — pure read/audit.

```bash
# Full function config
aws lambda get-function-configuration --function-name <function-name>

# Environment variables (these become application.properties / env vars on EC2)
aws lambda get-function-configuration --function-name <function-name> --query "Environment.Variables"

# IAM execution role — you'll replicate its permissions into an EC2 instance profile
aws lambda get-function-configuration --function-name <function-name> --query "Role"
aws iam get-role --role-name <execution-role-name>
aws iam list-attached-role-policies --role-name <execution-role-name>
aws iam list-role-policies --role-name <execution-role-name>   # inline policies too

# VPC config (if the Lambda runs inside a VPC — determines EC2 subnet/SG placement)
aws lambda get-function-configuration --function-name <function-name> --query "VpcConfig"

# Memory/timeout — informs EC2 instance sizing (Lambda memory maps roughly
# to allocated vCPU too; a 1024MB Lambda ≈ a small-to-mid EC2 instance is a
# reasonable starting point, but load-test before committing)
aws lambda get-function-configuration --function-name <function-name> --query "[MemorySize,Timeout]"

# Download the actual deployed code (to diff against your source repo —
# confirms you're migrating what's ACTUALLY running, not what you think is)
aws lambda get-function --function-name <function-name> --query "Code.Location" --output text
# curl the returned presigned URL to download the .zip/.jar
```

Write down, concretely:
- [ ] Handler class + method (e.g. `com.company.OrderHandler::handleRequest`)
- [ ] Runtime version (`java11`, `java17`, `java21`) — match this on EC2's JVM
- [ ] All environment variables and their values
- [ ] Execution role's full permission set
- [ ] VPC/subnet/security group (if applicable)
- [ ] Memory + timeout
- [ ] Every trigger attached (see section 2)
- [ ] Any Lambda layers used (shared JARs — these become regular dependencies)
- [ ] Reserved concurrency / provisioned concurrency settings (informs whether
      you need more than one EC2 instance eventually — you said single
      instance for now, but note this for future scaling conversations)

---

## 2. Trigger-by-trigger replacement design

Pick the section(s) matching your Lambda's actual trigger(s). Most
production Lambdas have exactly one; some have more than one type stacked.

### 2a. API Gateway (REST or HTTP API) → ALB + embedded HTTP server on EC2

**Today:** API Gateway receives the HTTP request, invokes the Lambda
synchronously, Lambda returns a response, API Gateway returns it to the
caller.

**Replacement:** Run the Java code as a real, always-on HTTP server
process on the EC2 instance, put an Application Load Balancer (ALB) in
front of it (or route Route 53 directly to the instance if you truly don't
need load balancing yet — not recommended even for one instance, since ALB
gives you health checks and a stable DNS name independent of instance IP).

Steps:
1. **Unwrap the Lambda handler into a real HTTP endpoint.** Your Lambda
   handler class currently implements
   `com.amazonaws.services.lambda.runtime.RequestHandler<APIGatewayProxyRequestEvent, APIGatewayProxyResponseEvent>`
   (or the newer `RequestStreamHandler`). That code needs to become a
   Spring Boot `@RestController` (recommended — most Java teams already
   know it) or a plain embedded Jetty/Undertow servlet if you want to avoid
   the Spring dependency. Concretely:
   ```java
   // Before (Lambda):
   public class OrderHandler implements RequestHandler<APIGatewayProxyRequestEvent, APIGatewayProxyResponseEvent> {
       public APIGatewayProxyResponseEvent handleRequest(APIGatewayProxyRequestEvent event, Context context) {
           String body = event.getBody();
           // business logic
           return new APIGatewayProxyResponseEvent().withStatusCode(200).withBody(result);
       }
   }

   // After (Spring Boot on EC2):
   @RestController
   public class OrderController {
       @PostMapping("/orders")
       public ResponseEntity<String> createOrder(@RequestBody String body) {
           // SAME business logic, unchanged
           return ResponseEntity.ok(result);
       }
   }
   ```
   The actual business logic in the middle should move over close to
   unchanged — only the request/response plumbing at the edges changes.
2. **Map API Gateway's route configuration to your new controller's
   routes.** Check API Gateway's resource/method configuration
   (`aws apigateway get-resources --rest-api-id <id>`) and make sure every
   path + HTTP verb combination API Gateway currently routes is implemented
   as a matching `@RequestMapping` in your new app.
3. **Reproduce anything API Gateway was doing FOR you** — this is the part
   people forget, because API Gateway silently provides things a raw HTTP
   server doesn't:
   - **Request validation/models** — if API Gateway had request validation
     configured, that logic needs to move into your app (e.g. `@Valid` +
     Bean Validation annotations in Spring).
   - **API keys / usage plans / throttling** — replace with an ALB
     listener rule + WAF rate-based rule, or application-level rate
     limiting (e.g. Bucket4j), depending on what you actually need.
   - **CORS configuration** — reproduce explicitly in your app
     (`@CrossOrigin` in Spring, or a `CorsFilter`) — this is a very common
     "worked in Lambda, broken on EC2" gap.
   - **Custom authorizers (Lambda authorizer / Cognito authorizer)** — if
     API Gateway was doing auth via a Lambda authorizer or Cognito
     integration, you now need that check inside your app itself (a
     Spring Security filter validating the same JWT/token), since there's
     no API Gateway layer doing it for you anymore.
4. **Put an ALB in front of the EC2 instance:**
   ```bash
   aws elbv2 create-target-group --name order-service-tg --protocol HTTP --port 8080 \
     --vpc-id <vpc-id> --target-type instance \
     --health-check-path /health --health-check-interval-seconds 15

   aws elbv2 register-targets --target-group-arn <tg-arn> --targets Id=<ec2-instance-id>

   aws elbv2 create-load-balancer --name order-service-alb --subnets <subnet-1> <subnet-2> \
     --security-groups <alb-sg-id> --scheme internet-facing

   aws elbv2 create-listener --load-balancer-arn <alb-arn> --protocol HTTP --port 80 \
     --default-actions Type=forward,TargetGroupArn=<tg-arn>
   ```
5. **Add a real `/health` endpoint** in your app for the ALB health check —
   Lambda never needed this (API Gateway just trusted the invoke
   succeeded); a long-running process needs an explicit liveness signal.
6. **DNS cutover** — point your existing API Gateway custom domain's DNS
   record (or a new record) at the ALB, OR keep API Gateway in front and
   change its integration type from "Lambda proxy" to "HTTP proxy"
   pointing at the ALB/EC2 — this lets you keep API Gateway's throttling/
   API-key features if you still want them, while the actual compute moves
   to EC2. Decide which based on whether you want to keep API Gateway at
   all (see the cutover section for a phased approach either way).

### 2b. S3 event notification → S3 → SQS → polling consumer on EC2

**Today:** S3 PUT/DELETE (or other) event fires directly, invokes Lambda
asynchronously with the S3 event payload.

**Replacement:** EC2 has no way to receive a push notification from S3
directly — there's no equivalent of a Lambda's event-source subscription
for a plain EC2 process. The standard, AWS-recommended pattern is to
insert an SQS queue between S3 and your EC2 app, and have your app poll
the queue.

Steps:
1. **Create an SQS queue** and configure the S3 bucket to publish
   notifications to it (S3 → SNS → SQS is the classic fan-out pattern if
   multiple consumers need the same event; S3 → SQS directly is fine for a
   single consumer):
   ```bash
   aws sqs create-queue --queue-name s3-event-queue

   # Get the queue ARN, then attach a policy allowing S3 to send to it
   aws sqs get-queue-attributes --queue-url <queue-url> --attribute-names QueueArn
   ```
   Then configure the bucket notification (via console or):
   ```bash
   aws s3api put-bucket-notification-configuration --bucket <bucket-name> \
     --notification-configuration '{
       "QueueConfigurations": [{
         "QueueArn": "<sqs-queue-arn>",
         "Events": ["s3:ObjectCreated:*"]
       }]
     }'
   ```
2. **Rewrite the Lambda handler as a polling loop.** Your Lambda handler
   currently implements `RequestHandler<S3Event, Void>` (or similar) and
   gets invoked once per event. On EC2, write a long-running loop that
   polls SQS, parses the S3 event payload out of the SQS message body
   (S3→SQS wraps the event as JSON inside the SQS message), and calls the
   same business logic:
   ```java
   // Runs as a background thread / dedicated service class in your app
   SqsClient sqs = SqsClient.create();
   while (running) {
       ReceiveMessageRequest req = ReceiveMessageRequest.builder()
           .queueUrl(queueUrl)
           .maxNumberOfMessages(10)
           .waitTimeSeconds(20)  // long polling — avoids hammering SQS with empty polls
           .build();
       for (Message msg : sqs.receiveMessage(req).messages()) {
           S3EventNotification event = S3EventNotification.fromJson(msg.body());
           // SAME business logic your Lambda handler had
           processS3Event(event);
           sqs.deleteMessage(DeleteMessageRequest.builder()
               .queueUrl(queueUrl).receiptHandle(msg.receiptHandle()).build());
       }
   }
   ```
3. **Replicate retry/DLQ behavior.** Lambda's async invocation had its own
   retry count + optional Dead Letter Queue/on-failure destination. Set an
   SQS redrive policy (max receive count → DLQ) to reproduce this:
   ```bash
   aws sqs set-queue-attributes --queue-url <queue-url> --attributes '{
     "RedrivePolicy": "{\"deadLetterTargetArn\":\"<dlq-arn>\",\"maxReceiveCount\":\"3\"}"
   }'
   ```
4. **Run the polling loop as a systemd-managed background thread** inside
   the same app (see section 4), or as a separate systemd service if you'd
   rather isolate it from the HTTP-serving process.

### 2c. SQS trigger → same queue, EC2 polls instead of Lambda's event-source mapping

**Today:** Lambda has an event-source mapping polling SQS on your behalf,
invoking the Lambda in batches.

**Replacement:** This is actually the simplest migration — you're removing
Lambda's *managed* polling and replacing it with your *own* polling loop
against the exact same queue. No new AWS resources needed.

1. Get the existing queue URL/ARN from the event-source mapping you found
   in the inventory step (`list-event-source-mappings`).
2. Write the same polling-loop pattern shown in 2b, pointed at this
   existing queue.
3. **Match the batch size and visibility timeout** the Lambda's
   event-source mapping was using (`BatchSize`, and the queue's own
   `VisibilityTimeout`) so you don't accidentally process messages slower
   or faster than before, causing unexpected backlog or duplicate
   processing.
4. Remove the Lambda's event-source mapping once EC2 is confirmed working
   (`aws lambda delete-event-source-mapping --uuid <mapping-uuid>`) — until
   then, BOTH will be polling the same queue and racing for messages if you
   don't disable one first (see cutover section — don't run both against
   the same queue simultaneously in production).

### 2d. SNS trigger → SNS → SQS subscription → polling consumer on EC2

**Today:** SNS topic publishes a message, Lambda is subscribed directly as
an SNS subscription target, invoked once per published message.

**Replacement:** Same problem as S3 — EC2 can't be a direct SNS
subscription endpoint the way Lambda can. Subscribe an SQS queue to the
SNS topic instead, then poll that queue exactly like 2b/2c.

```bash
aws sqs create-queue --queue-name sns-fanout-queue
aws sns subscribe --topic-arn <topic-arn> --protocol sqs --notification-endpoint <queue-arn>
```
Then apply the same polling-loop code from 2b, adjusted for SNS's message
envelope format (SNS wraps the actual message in its own JSON structure
inside the SQS body — `Message` field holds the original payload).

### 2e. EventBridge (CloudWatch Events) scheduled rule → cron on EC2

**Today:** An EventBridge rule on a `rate()` or `cron()` schedule invokes
the Lambda directly, no queue involved.

**Replacement:** This is the easiest one — just run the same logic on a
schedule using the OS's own scheduler, since there's no event payload
routing problem at all (a scheduled trigger doesn't need SQS in between).

Options, in order of how idiomatic they are for a systemd-managed EC2 app:
1. **systemd timer** (preferred over raw cron on a modern EC2/Linux setup —
   integrates with `systemctl status`, journald logging, and your existing
   systemd service):
   ```ini
   # /etc/systemd/system/order-cleanup.timer
   [Unit]
   Description=Run order cleanup job on schedule

   [Timer]
   OnCalendar=*-*-* 02:00:00   # daily at 2am, matching your EventBridge cron expression
   Persistent=true             # runs missed jobs if instance was down at trigger time

   [Install]
   WantedBy=timers.target
   ```
   ```ini
   # /etc/systemd/system/order-cleanup.service
   [Unit]
   Description=Order cleanup batch job

   [Service]
   Type=oneshot
   ExecStart=/usr/bin/java -jar /opt/app/order-cleanup.jar
   User=appuser
   ```
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now order-cleanup.timer
   ```
2. **Plain crontab** if you want the absolute simplest option and don't
   need systemd's extra features:
   ```bash
   # crontab -e (as the app user)
   0 2 * * * /usr/bin/java -jar /opt/app/order-cleanup.jar >> /var/log/order-cleanup.log 2>&1
   ```
3. **Convert the EventBridge cron expression correctly** — EventBridge
   uses a 6-field cron format (`cron(0 2 * * ? *)`) which is NOT the same
   as standard Unix cron's 5-field format. Translate carefully; a mismatch
   here silently changes your job's schedule.

### 2f. DynamoDB Streams trigger → EC2 polling the stream directly

**Today:** Lambda has an event-source mapping on the table's DynamoDB
Stream, invoked in batches as records change.

**Replacement:** DynamoDB Streams can be polled directly by your
application using the DynamoDB Streams SDK — no queue needed in between,
similar to SQS's own SDK-based polling:

```java
DynamoDbStreamsClient streamsClient = DynamoDbStreamsClient.create();
DescribeStreamResponse streamDesc = streamsClient.describeStream(
    DescribeStreamRequest.builder().streamArn(streamArn).build());

for (Shard shard : streamDesc.stream().shards()) {
    GetShardIteratorResponse iterResp = streamsClient.getShardIterator(
        GetShardIteratorRequest.builder()
            .streamArn(streamArn).shardId(shard.shardId())
            .shardIteratorType(ShardIteratorType.TRIM_HORIZON)
            .build());
    String iterator = iterResp.shardIterator();
    while (iterator != null) {
        GetRecordsResponse records = streamsClient.getRecords(
            GetRecordsRequest.builder().shardIterator(iterator).build());
        for (Record record : records.records()) {
            // SAME business logic your Lambda handler had
            processStreamRecord(record);
        }
        iterator = records.nextShardIterator();
        Thread.sleep(1000); // don't hammer the API — Lambda's polling had its own built-in pacing
    }
}
```
This is meaningfully more complex than Lambda's managed polling (you're
now responsible for shard discovery, iterator management, and handling
shard splits/merges as the table scales) — if your team isn't already
comfortable with this, consider using the **Kinesis Client Library (KCL)**,
which handles shard lifecycle management for you and is the standard
production pattern for self-managed stream consumers.

### 2g. Direct/manual invoke (`aws lambda invoke`, SDK calls from other services)

**Today:** Some other service or script calls the Lambda directly via the
Lambda Invoke API (synchronous or async).

**Replacement:** Depends entirely on what's calling it:
- If it's another internal service calling synchronously expecting a
  response → expose the same logic as an internal HTTP endpoint (same
  pattern as 2a) and update the caller to make an HTTP call instead of an
  `InvokeFunction` API call.
- If it's fire-and-forget/async → same SQS-polling pattern as above: have
  the caller send an SQS message instead of invoking Lambda, EC2 polls it.
- If it's from a script/cron elsewhere → point that script at a
  systemd-triggered endpoint or SSH-triggered script on the EC2 instance,
  or better, have it call the same HTTP endpoint over the network.

---

## 3. Java-specific code changes (applies regardless of trigger type)

1. **Remove the `RequestHandler`/`RequestStreamHandler` interface** —
   these come from `com.amazonaws:aws-lambda-java-core`, which you can
   drop as a dependency entirely once migrated.
2. **Business logic should move over almost unchanged.** If your original
   code was well-structured (handler class thin, actual logic in separate
   service classes), you're just deleting the handler wrapper and calling
   the same service classes from your new controller/poller/scheduled job.
   If the business logic was tangled into the handler method itself,
   extract it into a plain class first — this makes the migration mechanical
   instead of risky.
3. **Add connection pooling now that you can.** Lambda strongly discourages
   (or actively breaks) persistent DB connections across invocations —
   many Java Lambdas either reconnect every invocation or fight with
   RDS Proxy to work around this. On EC2, use a real connection pool
   (HikariCP is standard) initialized once at app startup:
   ```java
   HikariConfig config = new HikariConfig();
   config.setJdbcUrl(dbUrl);
   config.setMaximumPoolSize(10);
   // This pool now lives for the lifetime of the JVM process, not per-invocation
   ```
4. **JVM warm-up is now a one-time cost, not a per-invocation one.** If your
   Lambda had any hacks to reduce cold-start (SnapStart, provisioned
   concurrency, minimizing classpath size, avoiding reflection-heavy
   frameworks), most of that reasoning no longer applies — you can freely
   use full Spring Boot, larger dependency trees, etc., since the JVM only
   starts once at instance boot / service start, not per-request.
5. **Replace `context.getRemainingTimeInMillis()` and Lambda timeout logic**
   with normal Java timeout/thread-management patterns if your code used
   the Lambda `Context` object to self-manage time budgets.
6. **Logging** — Lambda's `LambdaLogger` (`context.getLogger()`) and
   CloudWatch Logs auto-wiring go away. Set up a normal logging framework
   (Logback/SLF4J) writing to a local file, and forward it to CloudWatch
   Logs via the **CloudWatch agent** (see section 5) if you want to keep
   centralized logging.
7. **Build a fat/executable JAR** instead of a Lambda deployment ZIP:
   ```xml
   <!-- Maven Shade or Spring Boot plugin, not the Lambda-specific packaging -->
   <plugin>
       <groupId>org.springframework.boot</groupId>
       <artifactId>spring-boot-maven-plugin</artifactId>
   </plugin>
   ```
   ```bash
   mvn clean package
   # produces target/your-app-1.0.0.jar — a normal runnable Spring Boot JAR
   ```

---

## 4. EC2 instance setup

1. **Launch the instance**, matching the Lambda's runtime version to an
   available JDK on the AMI (Amazon Linux 2023 or Ubuntu with Corretto/
   Temurin installed matching your Lambda's `java11`/`java17`/`java21`):
   ```bash
   aws ec2 run-instances \
     --image-id <ami-id> \
     --instance-type t3.medium \
     --key-name <your-key-pair> \
     --security-group-ids <sg-id> \
     --subnet-id <subnet-id> \
     --iam-instance-profile Name=<instance-profile-name> \
     --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=order-service-ec2}]'
   ```
2. **Create an IAM instance profile replicating the Lambda execution
   role's permissions** — this is a direct swap, not a redesign:
   ```bash
   aws iam create-role --role-name order-service-ec2-role \
     --assume-role-policy-document '{
       "Version": "2012-10-17",
       "Statement": [{
         "Effect": "Allow",
         "Principal": { "Service": "ec2.amazonaws.com" },
         "Action": "sts:AssumeRole"
       }]
     }'
   # Attach the SAME managed/inline policies the Lambda execution role had
   aws iam attach-role-policy --role-name order-service-ec2-role --policy-arn <same-policy-arn-as-lambda-role>

   aws iam create-instance-profile --instance-profile-name order-service-ec2-profile
   aws iam add-role-to-instance-profile --instance-profile-name order-service-ec2-profile --role-name order-service-ec2-role
   ```
   The instance's app code then uses the default AWS SDK credential chain
   (`DefaultCredentialsProvider`), which automatically picks up the instance
   profile's temporary credentials — same as how Lambda's SDK calls picked
   up the execution role automatically. No code change needed here beyond
   removing any Lambda-specific credential handling if it existed.
3. **Security group** — replicate what the Lambda's VPC config (if any) had
   attached, plus open whatever port your app listens on (e.g. 8080) to the
   ALB's security group only, not to the internet directly.
4. **Install the JDK and deploy the JAR:**
   ```bash
   # On the instance
   sudo yum install -y java-17-amazon-corretto   # match your Lambda's Java version
   sudo mkdir -p /opt/app
   # Copy your built JAR here (via S3, SCP, or your CI/CD pipeline)
   ```
5. **Create a systemd service** so the app survives reboots and restarts on
   crash, instead of running it manually in a terminal:
   ```ini
   # /etc/systemd/system/order-service.service
   [Unit]
   Description=Order Service (migrated from Lambda)
   After=network.target

   [Service]
   Type=simple
   User=appuser
   WorkingDirectory=/opt/app
   ExecStart=/usr/bin/java -jar /opt/app/order-service.jar
   Restart=on-failure
   RestartSec=5
   # Environment variables replicated from the Lambda's config (inventory step 1)
   Environment="DB_HOST=your-db-host"
   Environment="SOME_OTHER_VAR=value"
   # Or better: EnvironmentFile=/opt/app/.env  (keep secrets out of the unit file itself)

   [Install]
   WantedBy=multi-user.target
   ```
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now order-service.service
   sudo systemctl status order-service.service
   journalctl -u order-service.service -f   # tail logs live
   ```
6. **Don't put secrets directly in the unit file or a plain `.env` checked
   into anything.** Pull them from AWS Secrets Manager / SSM Parameter
   Store at startup instead, using the instance profile's permissions —
   this is a chance to actually improve on however the Lambda was getting
   its environment variables/secrets before.

---

## 5. Observability parity (don't lose visibility during the move)

Lambda gives you several things for free that a raw EC2 instance does not:

| Lambda gives you automatically | EC2 equivalent you must set up |
|---|---|
| CloudWatch Logs (auto-captured stdout/stderr) | Install the **CloudWatch agent**, configure it to tail your app's log file / journald output into a CloudWatch Log Group |
| Invocation count / error count / duration metrics | CloudWatch agent custom metrics, or instrument the app with Micrometer → CloudWatch, or just rely on ALB target group metrics (request count, latency, 5xx count) for HTTP-triggered cases |
| Automatic X-Ray tracing (if enabled) | Install the X-Ray daemon on the instance, or move to OpenTelemetry if you want to be less AWS-locked-in |
| Automatic restart on crash (new execution environment) | systemd's `Restart=on-failure` (already in the unit file above) |
| Automatic scaling to concurrent invocations | **None — this is the actual tradeoff of choosing a single instance.** State clearly to stakeholders that this migration trades elastic scaling for lower latency/cost, and revisit if traffic grows past what one instance can handle (ASG is the natural next step, out of scope for this single-instance migration). |

---

## 6. Cutover plan (don't just flip a switch)

1. **Deploy to EC2, keep the Lambda live and untouched.** Test the EC2
   version directly (curl the ALB, manually publish a test SQS message,
   whatever matches your trigger type) without touching production
   traffic yet.
2. **Run both in parallel, but only one actually processing real events at
   a time** — especially critical for poll-based triggers (SQS, DynamoDB
   Streams): if both Lambda's event-source mapping AND your EC2 poller are
   active against the same queue/stream simultaneously, you'll get
   duplicate processing or race conditions. Disable Lambda's event-source
   mapping (`aws lambda update-event-source-mapping --uuid <uuid> --no-enabled`)
   the moment you enable EC2's poller, don't run both live.
3. **For HTTP triggers**, shift traffic gradually if your DNS/API Gateway
   setup allows it (weighted Route 53 records, or API Gateway canary
   deployments) rather than an instant 100% cutover — lets you catch
   problems on a small percentage of real traffic first.
4. **Keep the Lambda deployed but disabled/unwired for a rollback window**
   (a week or two, depending on your risk tolerance) rather than deleting
   it immediately — if EC2 has a problem under real load that testing
   didn't catch, you can re-enable the Lambda trigger quickly while you fix
   the EC2 issue.
5. **Only after a confirmed stable period on EC2**, decommission the
   Lambda:
   ```bash
   aws lambda delete-event-source-mapping --uuid <mapping-uuid>   # if applicable
   aws lambda delete-function --function-name <function-name>
   # Clean up the now-unused Lambda execution role, if nothing else uses it
   aws iam detach-role-policy --role-name <execution-role-name> --policy-arn <policy-arn>
   aws iam delete-role --role-name <execution-role-name>
   ```

---

## 7. Rollback plan (write this before you cut over, not after something breaks)

- **HTTP trigger:** revert the DNS record / API Gateway integration back to
  the Lambda. Fast, since Lambda was left deployed and untouched.
- **Queue-based trigger (SQS/SNS/S3):** re-enable the Lambda's
  event-source mapping (or re-add the SNS/S3 subscription pointing at
  Lambda), and stop/disable the EC2 poller so they don't both consume the
  same messages.
- **Scheduled trigger:** re-enable the EventBridge rule's Lambda target,
  disable the systemd timer on EC2.
- **Any trigger type:** if the EC2 instance itself is unhealthy (not a
  logic bug, but infra — instance down, out of memory, etc.), the fastest
  rollback is re-enabling whatever Lambda path was disabled, since Lambda's
  infra reliability doesn't depend on anything you just built.

---

## 8. Final checklist before calling this done

- [ ] All environment variables/secrets present and correct on EC2
- [ ] IAM instance profile has equivalent (not more, not less) permissions
      vs. the original Lambda execution role
- [ ] Every trigger type has been replaced with its EC2 equivalent and
      tested with a real (non-production, if possible) event
- [ ] Logging is flowing somewhere you can actually see it in production
- [ ] Health check endpoint exists and the ALB target group shows healthy
      (HTTP triggers only)
- [ ] systemd service restarts automatically on crash and on instance
      reboot (`systemctl is-enabled order-service.service` → `enabled`)
- [ ] No duplicate processing window occurred during cutover (checked
      logs/metrics from both Lambda and EC2 during the parallel-run period)
- [ ] Rollback path tested at least once in a non-production environment,
      not just written down
- [ ] Lambda kept in a disabled-but-present state for the agreed rollback
      window before final deletion
- [ ] Stakeholders informed that this instance does not auto-scale —
      capacity planning is now a manual/manual-ASG concern going forward
