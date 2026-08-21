export type Json =
  | string
  | number
  | boolean
  | null
  | { [key: string]: Json | undefined }
  | Json[]

export type Database = {
  // Allows to automatically instantiate createClient with right options
  // instead of createClient<Database, { PostgrestVersion: 'XX' }>(URL, KEY)
  __InternalSupabase: {
    PostgrestVersion: "14.15"
  }
  public: {
    Tables: {
      _smoke: {
        Row: {
          created_at: string
          id: string
          note: string | null
          user_id: string
        }
        Insert: {
          created_at?: string
          id?: string
          note?: string | null
          user_id?: string
        }
        Update: {
          created_at?: string
          id?: string
          note?: string | null
          user_id?: string
        }
        Relationships: []
      }
      collect_run: {
        Row: {
          deal_ym: string
          error_code: string | null
          error_msg: string | null
          fetched_rows: number | null
          finished_at: string | null
          id: number
          inserted_count: number | null
          kind: string
          lawd_cd: string
          page_count: number | null
          started_at: string
          status: string
          total_count: number | null
          unchanged_count: number | null
          updated_count: number | null
        }
        Insert: {
          deal_ym: string
          error_code?: string | null
          error_msg?: string | null
          fetched_rows?: number | null
          finished_at?: string | null
          id?: never
          inserted_count?: number | null
          kind: string
          lawd_cd: string
          page_count?: number | null
          started_at?: string
          status: string
          total_count?: number | null
          unchanged_count?: number | null
          updated_count?: number | null
        }
        Update: {
          deal_ym?: string
          error_code?: string | null
          error_msg?: string | null
          fetched_rows?: number | null
          finished_at?: string | null
          id?: never
          inserted_count?: number | null
          kind?: string
          lawd_cd?: string
          page_count?: number | null
          started_at?: string
          status?: string
          total_count?: number | null
          unchanged_count?: number | null
          updated_count?: number | null
        }
        Relationships: []
      }
      deal_change_log: {
        Row: {
          changed_at: string
          changed_fields: string[]
          id: number
          old_row: Json
          rent_id: number | null
          run_id: number | null
          source_table: string
          trade_id: number | null
        }
        Insert: {
          changed_at?: string
          changed_fields: string[]
          id?: never
          old_row: Json
          rent_id?: number | null
          run_id?: number | null
          source_table: string
          trade_id?: number | null
        }
        Update: {
          changed_at?: string
          changed_fields?: string[]
          id?: never
          old_row?: Json
          rent_id?: number | null
          run_id?: number | null
          source_table?: string
          trade_id?: number | null
        }
        Relationships: [
          {
            foreignKeyName: "deal_change_log_rent_id_fkey"
            columns: ["rent_id"]
            isOneToOne: false
            referencedRelation: "rent"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "deal_change_log_run_id_fkey"
            columns: ["run_id"]
            isOneToOne: false
            referencedRelation: "collect_run"
            referencedColumns: ["id"]
          },
          {
            foreignKeyName: "deal_change_log_trade_id_fkey"
            columns: ["trade_id"]
            isOneToOne: false
            referencedRelation: "trade"
            referencedColumns: ["id"]
          },
        ]
      }
      rent: {
        Row: {
          apt_nm: string
          apt_seq: string
          build_year: number | null
          contract_term: string | null
          contract_type: string | null
          deal_date: string
          deal_ym: string | null
          deposit: number
          exclu_use_ar: number
          first_seen_at: string
          floor: number
          id: number
          is_current: boolean
          jibun: string | null
          last_seen_at: string
          monthly_rent: number
          pre_deposit: number | null
          pre_monthly_rent: number | null
          road_nm_full: string | null
          sgg_cd: string
          trade_count: number
          umd_nm: string | null
          use_rr_right: string | null
        }
        Insert: {
          apt_nm: string
          apt_seq: string
          build_year?: number | null
          contract_term?: string | null
          contract_type?: string | null
          deal_date: string
          deal_ym?: string | null
          deposit: number
          exclu_use_ar: number
          first_seen_at?: string
          floor: number
          id?: never
          is_current?: boolean
          jibun?: string | null
          last_seen_at?: string
          monthly_rent: number
          pre_deposit?: number | null
          pre_monthly_rent?: number | null
          road_nm_full?: string | null
          sgg_cd: string
          trade_count?: number
          umd_nm?: string | null
          use_rr_right?: string | null
        }
        Update: {
          apt_nm?: string
          apt_seq?: string
          build_year?: number | null
          contract_term?: string | null
          contract_type?: string | null
          deal_date?: string
          deal_ym?: string | null
          deposit?: number
          exclu_use_ar?: number
          first_seen_at?: string
          floor?: number
          id?: never
          is_current?: boolean
          jibun?: string | null
          last_seen_at?: string
          monthly_rent?: number
          pre_deposit?: number | null
          pre_monthly_rent?: number | null
          road_nm_full?: string | null
          sgg_cd?: string
          trade_count?: number
          umd_nm?: string | null
          use_rr_right?: string | null
        }
        Relationships: []
      }
      trade: {
        Row: {
          ambiguous_cancel: boolean
          apt_dong: string | null
          apt_nm: string
          apt_seq: string
          build_year: number | null
          buyer_gbn: string | null
          cdeal_day: string | null
          cdeal_type: string | null
          deal_amount: number
          deal_date: string
          deal_ym: string | null
          dealing_gbn: string | null
          estate_agent_sgg_nm: string | null
          exclu_use_ar: number
          first_seen_at: string
          floor: number
          id: number
          is_current: boolean
          jibun: string | null
          land_leasehold_gbn: string | null
          last_seen_at: string
          rgst_date: string | null
          road_nm: string | null
          road_nm_bonbun: string | null
          road_nm_bubun: string | null
          sgg_cd: string
          sler_gbn: string | null
          trade_count: number
          umd_cd: string | null
          umd_nm: string | null
        }
        Insert: {
          ambiguous_cancel?: boolean
          apt_dong?: string | null
          apt_nm: string
          apt_seq: string
          build_year?: number | null
          buyer_gbn?: string | null
          cdeal_day?: string | null
          cdeal_type?: string | null
          deal_amount: number
          deal_date: string
          deal_ym?: string | null
          dealing_gbn?: string | null
          estate_agent_sgg_nm?: string | null
          exclu_use_ar: number
          first_seen_at?: string
          floor: number
          id?: never
          is_current?: boolean
          jibun?: string | null
          land_leasehold_gbn?: string | null
          last_seen_at?: string
          rgst_date?: string | null
          road_nm?: string | null
          road_nm_bonbun?: string | null
          road_nm_bubun?: string | null
          sgg_cd: string
          sler_gbn?: string | null
          trade_count?: number
          umd_cd?: string | null
          umd_nm?: string | null
        }
        Update: {
          ambiguous_cancel?: boolean
          apt_dong?: string | null
          apt_nm?: string
          apt_seq?: string
          build_year?: number | null
          buyer_gbn?: string | null
          cdeal_day?: string | null
          cdeal_type?: string | null
          deal_amount?: number
          deal_date?: string
          deal_ym?: string | null
          dealing_gbn?: string | null
          estate_agent_sgg_nm?: string | null
          exclu_use_ar?: number
          first_seen_at?: string
          floor?: number
          id?: never
          is_current?: boolean
          jibun?: string | null
          land_leasehold_gbn?: string | null
          last_seen_at?: string
          rgst_date?: string | null
          road_nm?: string | null
          road_nm_bonbun?: string | null
          road_nm_bubun?: string | null
          sgg_cd?: string
          sler_gbn?: string | null
          trade_count?: number
          umd_cd?: string | null
          umd_nm?: string | null
        }
        Relationships: []
      }
    }
    Views: {
      [_ in never]: never
    }
    Functions: {
      [_ in never]: never
    }
    Enums: {
      [_ in never]: never
    }
    CompositeTypes: {
      [_ in never]: never
    }
  }
}

type DatabaseWithoutInternals = Omit<Database, "__InternalSupabase">

type DefaultSchema = DatabaseWithoutInternals[Extract<keyof Database, "public">]

export type Tables<
  DefaultSchemaTableNameOrOptions extends
    | keyof (DefaultSchema["Tables"] & DefaultSchema["Views"])
    | { schema: keyof DatabaseWithoutInternals },
  TableName extends DefaultSchemaTableNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof (DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"] &
        DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Views"])
    : never = never,
> = DefaultSchemaTableNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? (DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"] &
      DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Views"])[TableName] extends {
      Row: infer R
    }
    ? R
    : never
  : DefaultSchemaTableNameOrOptions extends keyof (DefaultSchema["Tables"] &
        DefaultSchema["Views"])
    ? (DefaultSchema["Tables"] &
        DefaultSchema["Views"])[DefaultSchemaTableNameOrOptions] extends {
        Row: infer R
      }
      ? R
      : never
    : never

export type TablesInsert<
  DefaultSchemaTableNameOrOptions extends
    | keyof DefaultSchema["Tables"]
    | { schema: keyof DatabaseWithoutInternals },
  TableName extends DefaultSchemaTableNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"]
    : never = never,
> = DefaultSchemaTableNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"][TableName] extends {
      Insert: infer I
    }
    ? I
    : never
  : DefaultSchemaTableNameOrOptions extends keyof DefaultSchema["Tables"]
    ? DefaultSchema["Tables"][DefaultSchemaTableNameOrOptions] extends {
        Insert: infer I
      }
      ? I
      : never
    : never

export type TablesUpdate<
  DefaultSchemaTableNameOrOptions extends
    | keyof DefaultSchema["Tables"]
    | { schema: keyof DatabaseWithoutInternals },
  TableName extends DefaultSchemaTableNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"]
    : never = never,
> = DefaultSchemaTableNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"][TableName] extends {
      Update: infer U
    }
    ? U
    : never
  : DefaultSchemaTableNameOrOptions extends keyof DefaultSchema["Tables"]
    ? DefaultSchema["Tables"][DefaultSchemaTableNameOrOptions] extends {
        Update: infer U
      }
      ? U
      : never
    : never

export type Enums<
  DefaultSchemaEnumNameOrOptions extends
    | keyof DefaultSchema["Enums"]
    | { schema: keyof DatabaseWithoutInternals },
  EnumName extends DefaultSchemaEnumNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof DatabaseWithoutInternals[DefaultSchemaEnumNameOrOptions["schema"]]["Enums"]
    : never = never,
> = DefaultSchemaEnumNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? DatabaseWithoutInternals[DefaultSchemaEnumNameOrOptions["schema"]]["Enums"][EnumName]
  : DefaultSchemaEnumNameOrOptions extends keyof DefaultSchema["Enums"]
    ? DefaultSchema["Enums"][DefaultSchemaEnumNameOrOptions]
    : never

export type CompositeTypes<
  PublicCompositeTypeNameOrOptions extends
    | keyof DefaultSchema["CompositeTypes"]
    | { schema: keyof DatabaseWithoutInternals },
  CompositeTypeName extends PublicCompositeTypeNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof DatabaseWithoutInternals[PublicCompositeTypeNameOrOptions["schema"]]["CompositeTypes"]
    : never = never,
> = PublicCompositeTypeNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? DatabaseWithoutInternals[PublicCompositeTypeNameOrOptions["schema"]]["CompositeTypes"][CompositeTypeName]
  : PublicCompositeTypeNameOrOptions extends keyof DefaultSchema["CompositeTypes"]
    ? DefaultSchema["CompositeTypes"][PublicCompositeTypeNameOrOptions]
    : never

export const Constants = {
  public: {
    Enums: {},
  },
} as const
