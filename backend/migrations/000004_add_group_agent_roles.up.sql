ALTER TABLE `group_agents` ADD COLUMN `role` VARCHAR(20) NOT NULL DEFAULT 'member' AFTER `token`;
