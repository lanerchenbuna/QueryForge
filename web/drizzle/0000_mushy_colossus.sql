CREATE TABLE `studio_runs` (
	`id` text PRIMARY KEY NOT NULL,
	`question` text NOT NULL,
	`status` text NOT NULL,
	`model` text NOT NULL,
	`row_count` integer DEFAULT 0 NOT NULL,
	`duration` text NOT NULL,
	`created_at` text DEFAULT CURRENT_TIMESTAMP NOT NULL
);
--> statement-breakpoint
CREATE TABLE `studio_sources` (
	`id` text PRIMARY KEY NOT NULL,
	`name` text NOT NULL,
	`source_type` text NOT NULL,
	`object_key` text NOT NULL,
	`size_bytes` integer NOT NULL,
	`table_count` integer DEFAULT 1 NOT NULL,
	`status` text DEFAULT 'ready' NOT NULL,
	`semantic_json` text NOT NULL,
	`reviewed` integer DEFAULT false NOT NULL,
	`contract_status` text DEFAULT 'pending' NOT NULL,
	`created_at` text DEFAULT CURRENT_TIMESTAMP NOT NULL
);
