-- 课程前置规则、按版本重放、学分与证书沿革（MySQL 8）
CREATE TABLE `rule_versions` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(100) NOT NULL,
  `status` varchar(20) NOT NULL DEFAULT 'draft',
  `courses` json NOT NULL,
  `prerequisites` json NOT NULL,
  `checksum` varchar(64) NOT NULL,
  `current_key` varchar(20) DEFAULT NULL,
  `created_at` varchar(40) NOT NULL,
  `published_at` varchar(40) DEFAULT NULL,
  `published_by` varchar(100) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `current_key` (`current_key`),
  KEY `ix_rule_versions_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `rule_runs` (
  `id` int NOT NULL AUTO_INCREMENT,
  `rule_version_id` int NOT NULL,
  `state` varchar(20) NOT NULL DEFAULT 'pending',
  `as_of` varchar(40) NOT NULL,
  `input_checksum` varchar(64) NOT NULL,
  `input` json NOT NULL,
  `result` json DEFAULT NULL,
  `error` text,
  `attempt` int NOT NULL DEFAULT 0,
  `progress` int NOT NULL DEFAULT 0,
  `total` int NOT NULL DEFAULT 0,
  `phase` varchar(40) DEFAULT '待核算',
  `trigger_reason` varchar(40) DEFAULT 'manual',
  `triggered_by` varchar(100) DEFAULT '',
  `created_at` varchar(40) NOT NULL,
  `started_at` varchar(40) DEFAULT NULL,
  `finished_at` varchar(40) DEFAULT NULL,
  `superseded_by` int DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_rule_runs_rule_version_id` (`rule_version_id`),
  KEY `ix_rule_runs_state` (`state`),
  CONSTRAINT `rule_runs_ibfk_1` FOREIGN KEY (`rule_version_id`) REFERENCES `rule_versions` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `credits` (
  `id` int NOT NULL AUTO_INCREMENT,
  `run_id` int NOT NULL,
  `rule_version_id` int NOT NULL,
  `learner` varchar(60) NOT NULL,
  `course` varchar(40) NOT NULL,
  `event_id` int NOT NULL,
  `event_code` varchar(60) NOT NULL,
  `occurred_at` varchar(40) NOT NULL,
  `status` varchar(20) NOT NULL DEFAULT 'active',
  `created_at` varchar(40) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_credit_run_learner_course` (`run_id`, `learner`, `course`),
  KEY `ix_credits_run_id` (`run_id`),
  KEY `ix_credits_rule_version_id` (`rule_version_id`),
  KEY `ix_credits_learner` (`learner`),
  KEY `ix_credits_status` (`status`),
  CONSTRAINT `credits_ibfk_1` FOREIGN KEY (`run_id`) REFERENCES `rule_runs` (`id`),
  CONSTRAINT `credits_ibfk_2` FOREIGN KEY (`rule_version_id`) REFERENCES `rule_versions` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `certificates` (
  `id` int NOT NULL AUTO_INCREMENT,
  `public_id` varchar(64) NOT NULL,
  `learner` varchar(60) NOT NULL,
  `rule_version_id` int NOT NULL,
  `run_id` int NOT NULL,
  `status` varchar(20) NOT NULL DEFAULT 'active',
  `active_key` varchar(120) DEFAULT NULL,
  `basis` json NOT NULL,
  `proof` varchar(64) NOT NULL,
  `issued_at` varchar(40) NOT NULL,
  `revoked_at` varchar(40) DEFAULT NULL,
  `revoked_by_run_id` int DEFAULT NULL,
  `revoke_reason` text,
  PRIMARY KEY (`id`),
  UNIQUE KEY `public_id` (`public_id`),
  UNIQUE KEY `active_key` (`active_key`),
  UNIQUE KEY `uq_certificate_run_learner` (`run_id`, `learner`),
  KEY `ix_certificates_learner` (`learner`),
  KEY `ix_certificates_rule_version_id` (`rule_version_id`),
  KEY `ix_certificates_status` (`status`),
  CONSTRAINT `certificates_ibfk_1` FOREIGN KEY (`rule_version_id`) REFERENCES `rule_versions` (`id`),
  CONSTRAINT `certificates_ibfk_2` FOREIGN KEY (`run_id`) REFERENCES `rule_runs` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `system_locks` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(40) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `name` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
