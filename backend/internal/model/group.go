package model

import "time"

type Group struct {
	ID          uint      `gorm:"primaryKey;autoIncrement" json:"-"`
	GroupID     string    `gorm:"column:group_id;type:varchar(36);uniqueIndex:uk_groups_group_id;not null" json:"group_id"`
	Name        string    `gorm:"column:name;type:varchar(255);default:''" json:"name"`
	Description string    `gorm:"column:description;type:text;not null" json:"description"`
	CreatedAt   time.Time `gorm:"column:created_at;type:datetime;not null" json:"created_at"`
	UpdatedAt   time.Time `gorm:"column:updated_at;type:datetime;not null" json:"updated_at"`
}

func (Group) TableName() string { return "groups" }

type GroupAgent struct {
	ID      uint      `gorm:"primaryKey;autoIncrement" json:"-"`
	GroupID string    `gorm:"column:group_id;type:varchar(36);not null" json:"group_id"`
	Token   string    `gorm:"column:token;type:varchar(64);not null" json:"token"`
	Role    string    `gorm:"column:role;type:varchar(20);default:member" json:"role"`
	AddedAt time.Time `gorm:"column:added_at;type:datetime;not null" json:"added_at"`
}

func (GroupAgent) TableName() string { return "group_agents" }
