-- groups table
CREATE TABLE IF NOT EXISTS `groups` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `group_id` VARCHAR(36) NOT NULL,
    `name` VARCHAR(255) NOT NULL DEFAULT '',
    `description` TEXT NOT NULL,
    `created_at` DATETIME NOT NULL,
    `updated_at` DATETIME NOT NULL,
    UNIQUE INDEX `uk_groups_group_id` (`group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- group_agents table
CREATE TABLE IF NOT EXISTS `group_agents` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `group_id` VARCHAR(36) NOT NULL,
    `token` VARCHAR(64) NOT NULL,
    `added_at` DATETIME NOT NULL,
    INDEX `idx_group_agents_group_id` (`group_id`),
    INDEX `idx_group_agents_token` (`token`),
    UNIQUE INDEX `uk_group_agents_group_token` (`group_id`, `token`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
