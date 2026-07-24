CREATE TABLE `studio_domains` (
	`id` text PRIMARY KEY NOT NULL,
	`name` text NOT NULL,
	`slug` text NOT NULL,
	`description` text DEFAULT '' NOT NULL,
	`owner` text DEFAULT 'Workspace admin' NOT NULL,
	`status` text DEFAULT 'draft' NOT NULL,
	`is_sample` integer DEFAULT false NOT NULL,
	`created_at` text DEFAULT CURRENT_TIMESTAMP NOT NULL,
	`updated_at` text DEFAULT CURRENT_TIMESTAMP NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX `studio_domains_slug_idx` ON `studio_domains` (`slug`);--> statement-breakpoint
CREATE INDEX `studio_domains_created_at_idx` ON `studio_domains` (`created_at`);--> statement-breakpoint
ALTER TABLE `studio_runs` ADD `domain_id` text DEFAULT 'domain_anime_streaming' NOT NULL;--> statement-breakpoint
CREATE INDEX `studio_runs_domain_id_idx` ON `studio_runs` (`domain_id`);--> statement-breakpoint
ALTER TABLE `studio_sources` ADD `domain_id` text DEFAULT 'domain_anime_streaming' NOT NULL;--> statement-breakpoint
CREATE INDEX `studio_sources_domain_id_idx` ON `studio_sources` (`domain_id`);